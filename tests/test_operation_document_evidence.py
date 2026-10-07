from __future__ import annotations

import copy
import hashlib
import importlib.util
import io
import json
import pathlib
import socket
import tempfile
import time
import unittest
import zipfile
from unittest import mock
from xml.sax.saxutils import escape


ROOT = pathlib.Path(__file__).parents[1]
MODULE_PATH = ROOT / "scripts/operation_document_evidence.py"
SPEC = importlib.util.spec_from_file_location("operation_document_evidence", MODULE_PATH)
assert SPEC and SPEC.loader
DOCS = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(DOCS)


def _table(rows: list[list[str]]) -> str:
    return "<table>" + "".join(
        "<tr>" + "".join(f"<td>{value}</td>" for value in row) + "</tr>" for row in rows
    ) + "</table>"


def _page(name: str = "헬스체크 조회", key: str = "9001") -> bytes:
    return (
        '<input type="hidden" id="publicDataPk" value="10001">'
        '<input type="hidden" id="publicDataDetailPk" value="doc:one">'
        '<select id="open_api_detail_select">'
        f'<option value="{key}">{name}</option>'
        '</select>'
    ).encode()


def _detail(parameter_rows: list[list[str]] | None = None) -> bytes:
    header = ["항목명(영문)", "항목명(국문)", "항목크기", "항목구분", "샘플데이터", "항목설명"]
    rows = parameter_rows or [
        ["ServiceKey", "서비스키", "4", "필수", "DO_NOT_PERSIST_AUTH", "인증키"],
        ["pageNo", "페이지 번호", "4", "옵", "DO_NOT_PERSIST_SAMPLE", "페이지 번호"],
    ]
    response = [["resultCode", "결과코드", "2", "필수", "DO_NOT_PERSIST_RESPONSE", "결과"]]
    return (_table([header, *rows]) + _table([header, *response])).encode()


def _docx() -> bytes:
    def cell(value: str) -> str:
        return f"<w:tc><w:p><w:r><w:t>{escape(value)}</w:t></w:r></w:p></w:tc>"

    def table(rows: list[list[str]]) -> str:
        return "<w:tbl>" + "".join("<w:tr>" + "".join(cell(value) for value in row) + "</w:tr>" for row in rows) + "</w:tbl>"

    header = ["항목명(영문)", "항목명(국문)", "항목크기", "항목구분", "샘플데이터", "항목설명"]
    request = [
        ["ServiceKey", "서비스키", "400", "필수", "DO_NOT_PERSIST_GUIDE_SAMPLE", "인증키"],
        ["pageNo", "페이지 번호", "5", "선택", "DO_NOT_PERSIST_GUIDE_SAMPLE", "페이지 번호"],
    ]
    response = [["resultCode", "결과코드", "2", "필수", "DO_NOT_PERSIST_RESPONSE", "결과"]]
    service = table([["인터페이스 표준", "REST (GET, POST, PUT, DELETE)"]])
    operation = table([
        ["오퍼레이션 정보", "오퍼레이션 번호", "1", "오퍼레이션명(국문)", "헬스체크 조회"],
        ["오퍼레이션 유형", "조회(목록)"],
    ])
    uri = table([["https://apis.data.go.kr/demo/health?ServiceKey=DO_NOT_PERSIST_URL&pageNo=3"]])
    xml = (
        '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body>'
        + service + operation + table([header, *request]) + table([header, *response]) + uri
        + "</w:body></w:document>"
    ).encode()
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("word/document.xml", xml)
    return buffer.getvalue()


def _docx_two_operations() -> bytes:
    def cell(value: str) -> str:
        return f"<w:tc><w:p><w:r><w:t>{escape(value)}</w:t></w:r></w:p></w:tc>"

    def table(rows: list[list[str]]) -> str:
        return "<w:tbl>" + "".join("<w:tr>" + "".join(cell(value) for value in row) + "</w:tr>" for row in rows) + "</w:tbl>"

    header = ["항목명(영문)", "항목명(국문)", "항목크기", "항목구분", "샘플데이터", "항목설명"]
    operation_one = table([["오퍼레이션 정보", "오퍼레이션명(국문)", "헬스체크 조회"], ["오퍼레이션 유형", "조회(목록)"]])
    request_one = table([header, ["firstOnlyParam", "첫 번째 입력", "8", "필수", "PRIVATE_FIRST_SAMPLE", "입력"]])
    operation_two = table([["오퍼레이션 정보", "오퍼레이션명(국문)", "다른 작업 조회"], ["오퍼레이션 유형", "조회(목록)"]])
    request_two = table([header, ["secondOnlyParam", "두 번째 입력", "9", "필수", "PRIVATE_SECOND_SAMPLE", "입력"]])
    response_two = table([header, ["borrowedResponseName", "다른 응답", "10", "필수", "PRIVATE_RESPONSE", "응답"]])
    other_url = table([["https://apis.data.go.kr/demo/other?secondOnlyParam=PRIVATE_QUERY"]])
    xml = (
        '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body>'
        + operation_one + request_one + operation_two + request_two + response_two + other_url
        + "</w:body></w:document>"
    ).encode()
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("word/document.xml", xml)
    return buffer.getvalue()


def _docx_with_contract_metadata() -> bytes:
    def cell(value: str) -> str:
        return f"<w:tc><w:p><w:r><w:t>{escape(value)}</w:t></w:r></w:p></w:tc>"

    def table(rows: list[list[str]]) -> str:
        return "<w:tbl>" + "".join("<w:tr>" + "".join(cell(value) for value in row) + "</w:tr>" for row in rows) + "</w:tbl>"

    service = table([["API 호출 제한", "1,000회/일"]])
    operation = table([["오퍼레이션 정보", "오퍼레이션명(국문)", "헬스체크 조회"], ["오퍼레이션 유형", "조회(목록)"]])
    header = ["항목명(영문)", "항목명(국문)", "항목크기", "항목구분", "샘플데이터", "항목설명", "자료형", "허용값", "기본값"]
    request = table([
        header,
        ["pageNo", "페이지 번호", "0", "선택", "PRIVATE_SAMPLE", "페이지 번호", "integer", "0,1,2", "0"],
        ["enabled", "활성화", "1", "선택", "true", "활성화 여부", "boolean", "true,false", "false"],
        ["emptyValue", "빈 값", "0", "선택", "PRIVATE_EMPTY_SAMPLE", "빈 문자열", "string", "", '""'],
        ["privateValue", "비공개", "32", "선택", "PRIVATE_EXAMPLE", "예시 값", "string", "", "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"],
    ])
    response = table([["항목명(영문)", "항목명(국문)", "항목크기", "항목구분", "샘플데이터", "항목설명"], ["resultCode", "결과", "2", "필수", "PRIVATE_RESPONSE", "결과"]])
    xml = (
        '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body>'
        + service + operation + request + response + "</w:body></w:document>"
    ).encode()
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("word/document.xml", xml)
    return buffer.getvalue()


def _operation() -> dict:
    return {
        "operation_id": "a" * 64,
        "protocol": "REST",
        "provenance": {
            "provider": "data.go.kr", "dataset_id": "10001", "operation_name": "헬스체크 조회",
            "source_system": "data.go.kr", "upstream_operation_key": "9001",
        },
        "transport": {"endpoint": "https://apis.data.go.kr/demo/health"},
    }


def _swagger_page(document: dict, dataset_id: str = "10001") -> bytes:
    embedded = json.dumps(document, ensure_ascii=False, separators=(",", ":"))
    return (
        f'<input type="hidden" id="publicDataPk" value="{dataset_id}">'
        '<input type="hidden" id="publicDataDetailPk" value="doc:one">'
        "<script>const swaggerJson = " + chr(96) + embedded + chr(96) + ";</script>"
    ).encode("utf-8")


def _swagger_document(name: str = "헬스체크 조회", *, security: bool = False) -> dict:
    document = {
        "swagger": "2.0",
        "host": "apis.data.go.kr/demo",
        "basePath": "",
        "schemes": ["https"],
        "produces": ["application/json"],
        "paths": {
            "/health": {
                "get": {
                    "summary": name,
                    "operationId": "health",
                    "parameters": [
                        {"name": "serviceKey", "in": "query", "required": True, "type": "string", "example": "PRIVATE_SPEC_EXAMPLE"},
                        {"name": "pageNo", "in": "query", "required": False, "type": "integer", "default": 0, "enum": [0, 1]},
                        {"name": "numOfRows", "in": "query", "required": False, "type": "integer"},
                    ],
                    "responses": {
                        "200": {
                            "schema": {
                                "type": "object",
                                "properties": {
                                    "header": {"type": "object", "properties": {"resultCode": {"type": "string"}}},
                                    "body": {"type": "object", "properties": {"totalCount": {"type": "integer"}, "items": {"type": "array", "items": {"$ref": "#/definitions/HealthItem"}}}},
                                },
                            }
                        }
                    },
                }
            }
        },
        "definitions": {"HealthItem": {"type": "object", "properties": {"name": {"type": "string"}}}},
    }
    if security:
        document["securityDefinitions"] = {"ServiceKey": {"type": "apiKey", "name": "serviceKey", "in": "query"}}
        document["paths"]["/health"]["get"]["security"] = [{"ServiceKey": []}]
    return document


class OperationDocumentEvidenceTest(unittest.TestCase):
    def test_parsed_facts_bind_exact_identity_and_leave_method_incomplete(self) -> None:
        evidence = DOCS.parse_evidence(
            _operation(), page_raw=_page(), detail_raw=_detail(), guide_raw=_docx(),
            page_retrieved_at="2026-10-07T03:00:00Z", detail_retrieved_at="2026-10-07T03:01:00Z", guide_retrieved_at="2026-10-07T03:02:00Z",
        )
        encoded = json.dumps(evidence, ensure_ascii=False)
        for sample in ("DO_NOT_PERSIST_AUTH", "DO_NOT_PERSIST_SAMPLE", "DO_NOT_PERSIST_GUIDE_SAMPLE", "DO_NOT_PERSIST_URL", "DO_NOT_PERSIST_RESPONSE"):
            self.assertNotIn(sample, encoded)
        self.assertEqual(evidence["identity"]["source_refs"][0]["locator"]["kind"], "html_select_option")
        self.assertEqual(evidence["transport"]["http_method"]["status"], "unknown")
        self.assertEqual(evidence["transport"]["http_method"]["authority_scope"], "service_level_only")
        self.assertEqual(evidence["effect"]["classification"], "read_only")
        params = {item["name"]: item for item in evidence["parameters"]}
        self.assertEqual(params["ServiceKey"]["requiredness"]["value"], "required")
        self.assertEqual(params["pageNo"]["requiredness"]["value"], "optional")
        self.assertEqual(params["pageNo"]["location"]["value"], "query")
        self.assertEqual(params["ServiceKey"]["size"]["status"], "conflict")
        self.assertEqual(params["ServiceKey"]["cardinality"]["maximum"], None)
        self.assertTrue(params["ServiceKey"]["sample"]["present"])
        self.assertFalse(params["ServiceKey"]["sample"]["value_stored"])
        self.assertEqual(params["ServiceKey"]["data_type"]["value"], None)
        self.assertEqual(params["ServiceKey"]["enum"]["status"], "not_established")
        self.assertEqual(params["ServiceKey"]["default"]["status"], "not_established")
        self.assertEqual(evidence["response_assertion"]["fields"], ["resultCode"])
        self.assertEqual(evidence["response_assertion"]["empty_result_semantics"]["status"], "unknown")

    def test_evidence_matches_versioned_schema_and_schema_rejects_sample_values(self) -> None:
        try:
            from jsonschema import Draft202012Validator
        except ImportError:
            self.skipTest("jsonschema is an optional release-validation dependency")
        schema = json.loads((ROOT / "schemas/datapan.operation-document-evidence.v1.schema.json").read_text(encoding="utf-8"))
        Draft202012Validator.check_schema(schema)
        evidence = DOCS.parse_evidence(
            _operation(), page_raw=_page(), detail_raw=_detail(), guide_raw=_docx(),
            page_retrieved_at="2026-10-07T03:00:00Z", detail_retrieved_at="2026-10-07T03:01:00Z", guide_retrieved_at="2026-10-07T03:02:00Z",
        )
        validator = Draft202012Validator(schema)
        self.assertEqual(list(validator.iter_errors(evidence)), [])
        broken = copy.deepcopy(evidence)
        broken["parameters"][0]["sample"]["value_stored"] = True
        self.assertTrue(list(validator.iter_errors(broken)))

    def test_inline_swagger_resolves_exact_registered_contract_without_storing_examples(self) -> None:
        raw = _swagger_page(_swagger_document())
        evidence = DOCS.parse_openapi_evidence(
            _operation(),
            page_raw=raw,
            page_retrieved_at="2026-10-07T03:00:00Z",
            page_media_type="text/html;charset=UTF-8",
        )
        encoded = json.dumps(evidence, ensure_ascii=False)
        self.assertNotIn("PRIVATE_SPEC_EXAMPLE", encoded)
        self.assertEqual(evidence["transport"]["http_method"]["value"], "GET")
        self.assertEqual(evidence["transport"]["http_method"]["authority_scope"], "operation_specific")
        self.assertEqual(evidence["transport"]["host"]["value"], "apis.data.go.kr")
        self.assertEqual(evidence["transport"]["path"]["value"], "/demo/health")
        self.assertEqual(evidence["authentication"]["status"], "unknown")
        params = {item["name"]: item for item in evidence["parameters"]}
        self.assertEqual(params["serviceKey"]["requiredness"]["value"], "required")
        self.assertEqual(params["serviceKey"]["location"]["value"], "query")
        self.assertEqual(params["serviceKey"]["data_type"]["value"], "string")
        self.assertEqual(params["pageNo"]["requiredness"]["value"], "optional")
        self.assertEqual(params["pageNo"]["cardinality"]["maximum"], 1)
        self.assertEqual(params["pageNo"]["default"]["value"], "0")
        self.assertEqual(params["pageNo"]["enum"]["values"], ["0", "1"])
        self.assertTrue(params["serviceKey"]["sample"]["present"])
        self.assertFalse(params["serviceKey"]["sample"]["value_stored"])
        self.assertIn("resultCode", evidence["response_assertion"]["fields"])
        self.assertIn("name", evidence["response_assertion"]["fields"])
        self.assertEqual(evidence["response_assertion"]["empty_result_semantics"]["status"], "unknown")
        locator = evidence["transport"]["http_method"]["source_refs"][0]["locator"]
        self.assertEqual(locator["kind"], "html_inline_json")
        self.assertEqual(locator["json_pointer"], "#/paths/~1health/get")
        self.assertLess(locator["byte_start"], locator["byte_end"])

    def test_inline_swagger_security_definition_documents_authentication(self) -> None:
        evidence = DOCS.parse_openapi_evidence(
            _operation(),
            page_raw=_swagger_page(_swagger_document(security=True)),
            page_retrieved_at="2026-10-07T03:00:00Z",
        )
        self.assertEqual(evidence["authentication"]["status"], "documented")
        self.assertEqual(evidence["authentication"]["mechanism"], "service_key")
        self.assertEqual(evidence["authentication"]["placement"], "query")
        self.assertEqual(evidence["authentication"]["parameter_names"], ["serviceKey"])

    def test_inline_swagger_credential_description_documents_authentication(self) -> None:
        document = _swagger_document()
        document["paths"]["/health"]["get"]["parameters"][0]["description"] = "공공데이터포털에서 받은 인증키를 입력합니다."
        evidence = DOCS.parse_openapi_evidence(
            _operation(),
            page_raw=_swagger_page(document),
            page_retrieved_at="2026-10-07T03:00:00Z",
        )
        auth = evidence["authentication"]
        self.assertEqual((auth["status"], auth["requirement"], auth["mechanism"], auth["placement"]), ("documented", "required", "service_key", "query"))
        pointers = {item["locator"].get("json_pointer") for item in auth["source_refs"] if item["locator"]["kind"] == "html_inline_json"}
        self.assertIn("#/paths/~1health/get/parameters/0/description", pointers)
        self.assertIn("#/paths/~1health/get/parameters/0/name", pointers)
        self.assertIn("#/paths/~1health/get/parameters/0/in", pointers)
        self.assertIn("#/paths/~1health/get/parameters/0/required", pointers)

    def test_inline_swagger_response_contract_keeps_http_codes_and_provider_codes_separate(self) -> None:
        document = _swagger_document()
        document["paths"]["/health"]["get"]["responses"]["C10"] = {"description": "INVALID_REQUEST_PARAMETER_ERROR"}
        evidence = DOCS.parse_openapi_evidence(
            _operation(),
            page_raw=_swagger_page(document),
            page_retrieved_at="2026-10-07T03:00:00Z",
        )
        contract = evidence["response_contract"]
        self.assertEqual(contract["accepted_http_status_codes"]["values"], [200])
        result_code = contract["provider_result_codes"]
        self.assertEqual(result_code["status"], "unknown")
        self.assertEqual(result_code["path"], {"kind": "json_pointer", "value": "#/header/resultCode"})
        self.assertEqual(result_code["value_type"], "string")
        self.assertEqual(result_code["success_values"]["status"], "unknown")
        self.assertEqual(result_code["error_values"]["status"], "unknown")
        self.assertEqual(contract["result_collection"]["path"], {"kind": "json_pointer", "value": "#/body/items"})
        self.assertEqual(contract["result_collection"]["container_path"], {"kind": "json_pointer", "value": "#/body"})
        self.assertIsNone(contract["result_collection"]["item_path"])
        self.assertEqual(contract["result_collection"]["value_type"], "array")
        fields = {item["path"]["value"]: item for item in contract["documented_fields"] if item["path"] and item["path"]["kind"] == "json_pointer"}
        self.assertEqual(fields["#/header/resultCode"]["value_type"], "string")
        self.assertEqual(contract["required_fields"], [])

    def test_labeled_success_response_example_emits_only_typed_result_code_literal(self) -> None:
        document = _swagger_document()
        response = document["paths"]["/health"]["get"]["responses"]["200"]
        response["description"] = "Successful response"
        response["examples"] = {"application/json": {"header": {"resultCode": "00", "resultMsg": "PRIVATE_RESPONSE_ROW"}, "body": {"items": [{"name": "PRIVATE_RESPONSE_ROW"}]}}}
        evidence = DOCS.parse_openapi_evidence(
            _operation(),
            page_raw=_swagger_page(document),
            page_retrieved_at="2026-10-07T03:00:00Z",
        )
        contract = evidence["response_contract"]["provider_result_codes"]
        self.assertEqual(contract["status"], "example_only")
        self.assertEqual(contract["evidence_strength"], "official_success_example")
        self.assertEqual(contract["success_values"]["status"], "example_only")
        self.assertEqual(contract["success_values"]["values"], ["00"])
        self.assertEqual(contract["error_values"]["status"], "unknown")
        self.assertEqual(contract["error_values"]["values"], [])
        pointers = {item["locator"].get("json_pointer") for item in contract["success_values"]["source_refs"]}
        self.assertIn("#/paths/~1health/get/responses/200/description", pointers)
        self.assertIn("#/paths/~1health/get/responses/200/examples/application~1json/header/resultCode", pointers)
        encoded = json.dumps(evidence, ensure_ascii=False)
        self.assertIn('"00"', encoded)
        self.assertNotIn("PRIVATE_RESPONSE_ROW", encoded)
        self.assertNotIn('"resultMsg"', encoded)

    def test_unlabeled_or_error_response_examples_do_not_establish_success_code(self) -> None:
        document = _swagger_document()
        response = document["paths"]["/health"]["get"]["responses"]["200"]
        response["description"] = "Error response"
        response["examples"] = {"application/json": {"header": {"resultCode": "00"}}}
        evidence = DOCS.parse_openapi_evidence(
            _operation(),
            page_raw=_swagger_page(document),
            page_retrieved_at="2026-10-07T03:00:00Z",
        )
        contract = evidence["response_contract"]["provider_result_codes"]
        self.assertEqual(contract["status"], "unknown")
        self.assertEqual(contract["success_values"]["status"], "unknown")
        self.assertEqual(contract["success_values"]["values"], [])

    def test_complete_closed_response_schema_can_mark_result_codes_not_applicable(self) -> None:
        document = _swagger_document()
        document["paths"]["/health"]["get"]["responses"]["200"]["schema"] = {
            "type": "object",
            "additionalProperties": False,
            "properties": {"status": {"type": "string"}},
        }
        evidence = DOCS.parse_openapi_evidence(
            _operation(),
            page_raw=_swagger_page(document),
            page_retrieved_at="2026-10-07T03:00:00Z",
        )
        contract = evidence["response_contract"]["provider_result_codes"]
        self.assertEqual(contract["status"], "not_applicable")
        self.assertEqual(contract["evidence_strength"], "complete_response_schema")
        self.assertTrue(contract["source_refs"])

    def test_open_response_schema_does_not_claim_result_codes_not_applicable(self) -> None:
        document = _swagger_document()
        document["paths"]["/health"]["get"]["responses"]["200"]["schema"] = {
            "type": "object", "properties": {"status": {"type": "string"}},
        }
        evidence = DOCS.parse_openapi_evidence(
            _operation(),
            page_raw=_swagger_page(document),
            page_retrieved_at="2026-10-07T03:00:00Z",
        )
        contract = evidence["response_contract"]["provider_result_codes"]
        self.assertEqual(contract["status"], "unknown")
        self.assertEqual(contract["evidence_strength"], "unknown")

    def test_xml_collection_uses_absolute_required_container_and_relative_item_path(self) -> None:
        document = _swagger_document()
        document["produces"] = ["application/xml"]
        namespace = "urn:datapan:test"
        document["paths"]["/health"]["get"]["responses"]["200"]["schema"] = {
            "type": "object", "xml": {"name": "response", "namespace": namespace},
            "required": ["body"],
            "properties": {
                "body": {
                    "type": "object", "xml": {"name": "body", "namespace": namespace}, "required": ["items"],
                    "properties": {
                        "items": {
                            "type": "array", "xml": {"name": "items", "namespace": namespace, "wrapped": True},
                            "items": {"type": "object", "xml": {"name": "item", "namespace": namespace}},
                        }
                    },
                }
            },
        }
        evidence = DOCS.parse_openapi_evidence(
            _operation(),
            page_raw=_swagger_page(document),
            page_retrieved_at="2026-10-07T03:00:00Z",
        )
        collection = evidence["response_contract"]["result_collection"]
        self.assertEqual(collection["status"], "documented")
        self.assertIsNone(collection["path"])
        self.assertEqual(collection["container_path"], {"kind": "xml_qname_path", "segments": [
            {"namespace": namespace, "local_name": "response"},
            {"namespace": namespace, "local_name": "body"},
            {"namespace": namespace, "local_name": "items"},
        ]})
        self.assertEqual(collection["item_path"], {"kind": "xml_qname_path", "segments": [{"namespace": namespace, "local_name": "item"}]})
        self.assertEqual(collection["container_cardinality"]["status"], "documented")
        self.assertEqual((collection["container_cardinality"]["minimum"], collection["container_cardinality"]["maximum"]), (1, 1))

    def test_inline_swagger_rejects_dataset_endpoint_and_operation_name_mismatches(self) -> None:
        raw = _swagger_page(_swagger_document(), dataset_id="10002")
        with self.assertRaisesRegex(DOCS.EvidenceError, "catalogue_dataset_identity_mismatch"):
            DOCS.parse_openapi_evidence(_operation(), page_raw=raw, page_retrieved_at="2026-10-07T03:00:00Z")
        with self.assertRaisesRegex(DOCS.EvidenceError, "openapi_operation_identity_mismatch"):
            DOCS.parse_openapi_evidence(_operation(), page_raw=_swagger_page(_swagger_document("다른 작업")), page_retrieved_at="2026-10-07T03:00:00Z")

    def test_openapi_only_capture_is_resumable_and_receipt_binds_partial_sources(self) -> None:
        raw = _swagger_page(_swagger_document())
        headers = {"content-type": "text/html;charset=UTF-8", "date": "2026-10-07T03:00:00Z"}
        with tempfile.TemporaryDirectory() as tmp:
            capture_root = pathlib.Path(tmp) / "captures"
            with mock.patch.object(DOCS, "_fetch_official", return_value=(raw, headers, {})):
                result = DOCS._capture_one(_operation(), capture_root)
            self.assertEqual(result["status"], "parsed_with_unknowns")
            folder = capture_root / "10001" / "9001"
            receipt = json.loads((folder / "capture-receipt.json").read_text())
            self.assertEqual(receipt["status"], "acquired")
            self.assertIsNotNone(receipt["evidence_sha256"])
            present = {item["role"]: item["present"] for item in receipt["documents"]}
            self.assertEqual(present, {"catalogue": True, "operation_detail": False, "reference_guide": False})
            identity = DOCS._safe_operation_identity(_operation())
            self.assertEqual(DOCS._capture_work_status(identity, capture_root)["status"], "acquired")

    def test_parse_captures_publishes_digest_only_receipt(self) -> None:
        try:
            import jsonschema
        except ImportError:
            self.skipTest("jsonschema is an optional release-validation dependency")
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            capture_root = root / "captures"
            folder = capture_root / "10001" / "9001"
            folder.mkdir(parents=True)
            (folder / "catalogue.html").write_bytes(_swagger_page(_swagger_document()))
            folder.joinpath("catalogue.html").chmod(0o600)
            output = root / "evidence"
            operation = _operation()
            records = [operation]
            for index in range(1, 12662):
                other = copy.deepcopy(operation)
                other["operation_id"] = hashlib.sha256(str(index).encode()).hexdigest()
                other["provenance"]["dataset_id"] = str(20000 + index)
                other["provenance"]["upstream_operation_key"] = str(index)
                records.append(other)
            manifest = {
                "source_snapshot": {"path": "data/data-go-kr.registry.json", "sha256": "0520d0db0d9ee07b7cbccce0c08439d0b02be901bf10e8491187d96e59d7a0d0", "bytes": 139155499},
                "summary": {"api_operations": 12662},
                "operations": records,
            }
            parsed = DOCS._parse_existing_captures(manifest, capture_root, output)
            self.assertEqual(parsed, 1)
            receipt_path = output / "receipts/10001-9001.json"
            receipt = json.loads(receipt_path.read_text())
            schema = json.loads((ROOT / "schemas/datapan.operation-document-capture-receipt.v1.schema.json").read_text())
            jsonschema.Draft202012Validator(schema, format_checker=jsonschema.FormatChecker()).validate(receipt)
            encoded = json.dumps(receipt, ensure_ascii=False)
            self.assertNotIn("https://", encoded)
            self.assertNotIn("?", encoded)
            self.assertNotIn("example", encoded.casefold())
            self.assertEqual(receipt["evidence_sha256"], DOCS._sha256((output / "10001-9001.json").read_bytes()))

    def test_operation_selector_must_match_upstream_key_and_name(self) -> None:
        with self.assertRaisesRegex(DOCS.EvidenceError, "operation_selector_identity_ambiguous"):
            DOCS.parse_evidence(
                _operation(), page_raw=_page(name="다른 작업"), detail_raw=_detail(), guide_raw=_docx(),
                page_retrieved_at="2026-10-07T03:00:00Z", detail_retrieved_at="2026-10-07T03:01:00Z", guide_retrieved_at="2026-10-07T03:02:00Z",
            )

    def test_duplicate_document_operation_identity_is_ambiguous(self) -> None:
        guide = _docx()
        with zipfile.ZipFile(io.BytesIO(guide)) as archive:
            document = archive.read("word/document.xml")
        body = document.split(b"<w:body>", 1)[1].split(b"</w:body>", 1)[0]
        duplicate = document.replace(b"</w:body>", body + b"</w:body>")
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("word/document.xml", duplicate)
        with self.assertRaisesRegex(DOCS.EvidenceError, "guide_operation_identity_ambiguous"):
            DOCS.parse_evidence(
                _operation(), page_raw=_page(), detail_raw=_detail(), guide_raw=buffer.getvalue(),
                page_retrieved_at="2026-10-07T03:00:00Z", detail_retrieved_at="2026-10-07T03:01:00Z", guide_retrieved_at="2026-10-07T03:02:00Z",
            )

    def test_duplicate_parameter_rows_are_rejected_instead_of_overwritten(self) -> None:
        duplicated = [
            ["ServiceKey", "서비스키", "4", "필수", "PRIVATE_ONE", "인증키"],
            ["ServiceKey", "서비스키", "400", "선택", "PRIVATE_TWO", "충돌 행"],
        ]
        with self.assertRaisesRegex(DOCS.EvidenceError, "parameter_name_duplicate_in_source"):
            DOCS.parse_evidence(
                _operation(), page_raw=_page(), detail_raw=_detail(duplicated), guide_raw=_docx(),
                page_retrieved_at="2026-10-07T03:00:00Z", detail_retrieved_at="2026-10-07T03:01:00Z", guide_retrieved_at="2026-10-07T03:02:00Z",
            )

    def test_docx_facts_stop_before_the_next_operation_section(self) -> None:
        evidence = DOCS.parse_evidence(
            _operation(), page_raw=_page(), detail_raw=_detail(), guide_raw=_docx_two_operations(),
            page_retrieved_at="2026-10-07T03:00:00Z", detail_retrieved_at="2026-10-07T03:01:00Z", guide_retrieved_at="2026-10-07T03:02:00Z",
        )
        encoded = json.dumps(evidence, ensure_ascii=False)
        self.assertNotIn("secondOnlyParam", encoded)
        self.assertNotIn("borrowedResponseName", encoded)
        self.assertNotIn("PRIVATE_SECOND_SAMPLE", encoded)
        self.assertNotIn("PRIVATE_QUERY", encoded)
        self.assertEqual(evidence["response_assertion"]["fields"], [])
        self.assertEqual(evidence["transport"]["path"]["status"], "not_established")

    def test_explicit_type_enum_default_and_quota_keep_exact_provenance(self) -> None:
        evidence = DOCS.parse_evidence(
            _operation(), page_raw=_page(), detail_raw=_detail(), guide_raw=_docx_with_contract_metadata(),
            page_retrieved_at="2026-10-07T03:00:00Z", detail_retrieved_at="2026-10-07T03:01:00Z", guide_retrieved_at="2026-10-07T03:02:00Z",
        )
        params = {item["name"]: item for item in evidence["parameters"]}
        self.assertEqual(params["pageNo"]["data_type"], {"value": "integer", "status": "documented", "source_refs": params["pageNo"]["data_type"]["source_refs"]})
        self.assertEqual(params["pageNo"]["enum"]["values"], ["0", "1", "2"])
        self.assertEqual(params["pageNo"]["default"]["value"], "0")
        self.assertEqual(params["enabled"]["data_type"]["value"], "boolean")
        self.assertEqual(params["enabled"]["enum"]["values"], ["true", "false"])
        self.assertEqual(params["enabled"]["default"]["value"], "false")
        self.assertEqual(params["emptyValue"]["default"], {"value": "", "status": "documented", "source_refs": params["emptyValue"]["default"]["source_refs"]})
        self.assertEqual(params["emptyValue"]["size"]["value"], "0")
        self.assertEqual(params["emptyValue"]["cardinality"]["minimum"], 0)
        self.assertIsNone(params["emptyValue"]["cardinality"]["maximum"])
        self.assertEqual(params["privateValue"]["default"]["status"], "unknown")
        self.assertEqual(evidence["limits"]["provider_quota"]["value"], 1000)
        self.assertEqual(evidence["limits"]["provider_quota"]["unit"], "requests/day")
        encoded = json.dumps(evidence, ensure_ascii=False)
        for private in ("PRIVATE_SAMPLE", "PRIVATE_RESPONSE", "PRIVATE_EXAMPLE", "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"):
            self.assertNotIn(private, encoded)
        self.assertTrue(all(fact["source_refs"] for fact in (params["pageNo"]["data_type"], params["pageNo"]["enum"], params["pageNo"]["default"])))
        quota_without_period = DOCS._provider_quota_facts([[ ["API 호출 제한", "1,000회"] ]], source_id="guide", kind="docx_table_cell")
        self.assertEqual(quota_without_period["unit"], "requests")

    def test_docx_expansion_limit_rejects_zip_bomb(self) -> None:
        bomb = io.BytesIO()
        with zipfile.ZipFile(bomb, "w", zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("word/document.xml", b" " * 10000)
        with mock.patch.object(DOCS, "MAX_DOCX_EXPANDED_BYTES", 100):
            with self.assertRaisesRegex(DOCS.EvidenceError, "docx_expansion_limit"):
                DOCS._docx_tables(bomb.getvalue())

    def test_legacy_ole_operation_guide_is_explicitly_unsupported(self) -> None:
        with self.assertRaisesRegex(DOCS.EvidenceError, "guide_format_unsupported_legacy_ole_document"):
            DOCS._docx_tables(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\x00" * 64)
        self.assertEqual(DOCS._document_media_type("reference_guide", b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"), ("application/x-ole-storage", "binary"))

    def test_source_allowlist_rejects_query_redirect_and_private_dns(self) -> None:
        with self.assertRaisesRegex(DOCS.EvidenceError, "outside_official_allowlist"):
            DOCS._validate_official_url("https://127.0.0.1/data/1/openapi.do", purpose="catalogue")
        with self.assertRaisesRegex(DOCS.EvidenceError, "catalogue_query_not_allowed"):
            DOCS._validate_official_url("https://www.data.go.kr/data/1/openapi.do?key=private", purpose="catalogue")
        private_dns = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 443))]
        with self.assertRaisesRegex(DOCS.EvidenceError, "private_or_reserved"):
            DOCS._public_ip_for("www.data.go.kr", resolver=lambda *_args, **_kwargs: private_dns)
        with mock.patch.object(DOCS.multiprocessing, "get_context", side_effect=ValueError("fork unavailable")):
            with self.assertRaisesRegex(DOCS.EvidenceError, "source_deadline_process_unavailable"):
                DOCS._fetch_official("https://www.data.go.kr/data/10001/openapi.do", purpose="catalogue", max_bytes=128)

    def test_redirect_is_not_followed(self) -> None:
        class Response:
            status = 302
            headers = {}
            def getheader(self, *_args): return ""
        class Connection:
            def __init__(self, *_args, **_kwargs): self.sock = None; self._create_connection = None
            def request(self, *_args, **_kwargs): pass
            def getresponse(self): return Response()
            def close(self): pass
        with mock.patch.object(DOCS, "_public_ip_for", return_value="8.8.8.8"), mock.patch.object(DOCS.http.client, "HTTPSConnection", Connection):
            with self.assertRaisesRegex(DOCS.EvidenceError, "source_redirect_rejected"):
                DOCS._fetch_official("https://www.data.go.kr/data/10001/openapi.do", purpose="catalogue", max_bytes=128)

    def test_conflicting_content_length_framing_is_rejected(self) -> None:
        class Headers:
            def get_all(self, name): return ["1", "2"] if name == "Content-Length" else []
        class Response:
            status = 200
            headers = Headers()
            def getheader(self, name, default=""):
                return {"Content-Encoding": "identity"}.get(name, default)
        class Connection:
            def __init__(self, *_args, **_kwargs): self.sock = None; self._create_connection = None
            def request(self, *_args, **_kwargs): pass
            def getresponse(self): return Response()
            def close(self): pass
        resolver = lambda *_args, **_kwargs: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", 443))]
        with mock.patch.object(DOCS.http.client, "HTTPSConnection", Connection):
            with self.assertRaisesRegex(DOCS.EvidenceError, "source_framing_invalid"):
                DOCS._fetch_official("https://www.data.go.kr/data/10001/openapi.do", purpose="catalogue", max_bytes=128, resolver=resolver)

    def test_hard_deadline_covers_slow_dns_headers_and_trickled_body(self) -> None:
        class Headers:
            def get_all(self, _name): return []

        class Response:
            status = 200
            headers = Headers()
            def __init__(self, body_delay): self.body_delay = body_delay
            def getheader(self, name, default=""):
                return {"Content-Encoding": "identity", "Content-Length": ""}.get(name, default)
            def read(self, _size):
                if self.body_delay:
                    time.sleep(0.08)
                    return b"x"
                return b""

        class Connection:
            def __init__(self, *_args, **_kwargs):
                self.sock = None
                self._create_connection = None
                self.response_delay = False
                self.body_delay = False
            def request(self, *_args, **_kwargs): pass
            def getresponse(self):
                if self.response_delay:
                    time.sleep(1.0)
                return Response(self.body_delay)
            def close(self): pass

        url = "https://www.data.go.kr/data/10001/openapi.do"

        def run_with_timeout(stage):
            class StageConnection(Connection):
                def __init__(self, *args, **kwargs):
                    super().__init__(*args, **kwargs)
                    self.response_delay = stage == "headers"
                    self.body_delay = stage == "body"

            slow_resolver = lambda *_args, **_kwargs: (time.sleep(1.0), [])[1]
            public_resolver = lambda *_args, **_kwargs: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", 443))]
            started = time.monotonic()
            resolver = slow_resolver if stage == "dns" else public_resolver
            with mock.patch.object(DOCS.http.client, "HTTPSConnection", StageConnection):
                with self.assertRaisesRegex(DOCS.EvidenceError, "source_deadline_exceeded"):
                    DOCS._fetch_official(url, purpose="catalogue", max_bytes=16, deadline_seconds=0.2, resolver=resolver)
            self.assertLess(time.monotonic() - started, 1.0)

        for stage in ("dns", "headers", "body"):
            with self.subTest(stage=stage):
                run_with_timeout(stage)

    def test_full_registered_queue_keeps_safetydata_and_exclusions_separate(self) -> None:
        manifest = json.loads((ROOT / "reports/data-go-kr/operation-manifest.json").read_text(encoding="utf-8"))
        queue = DOCS.build_queue(manifest)
        self.assertEqual(len(queue), 12662)
        safetydata = [row for row in queue if row["source_profile_id"] == "safetydata_v1"]
        self.assertEqual(len(safetydata), 180)
        self.assertEqual(DOCS.SOURCE_PROFILES[safetydata[0]["source_profile_id"]]["host"], "www.safetydata.go.kr")
        self.assertEqual(manifest["summary"]["exclusions"]["link_operations"], 8871)
        self.assertEqual(manifest["summary"]["exclusions"]["operationless_catalog_entries"], 473)
        self.assertEqual(DOCS.select_queue_batch(queue, offset=3, limit=2), queue[3:5])
        with self.assertRaisesRegex(DOCS.EvidenceError, "queue_offset_out_of_range"):
            DOCS.select_queue_batch(queue, offset=len(queue), limit=1)
        with self.assertRaisesRegex(DOCS.EvidenceError, "queue_batch_limit_invalid"):
            DOCS.select_queue_batch(queue, offset=0, limit=DOCS.MAX_BATCH_OPERATIONS + 1)

    def test_reconciliation_emits_exact_registered_statuses_and_next_actions(self) -> None:
        manifest = json.loads((ROOT / "reports/data-go-kr/operation-manifest.json").read_text(encoding="utf-8"))
        queue = DOCS.build_queue(manifest)
        report, work_items = DOCS.reconcile(queue, ROOT / "reports/operation-document-evidence")
        self.assertEqual(sum(report["summary"]["statuses"].values()), len(queue))
        self.assertEqual(report["summary"]["statuses"]["parsed_with_unknowns"], 4)
        self.assertEqual(report["summary"]["statuses"]["pending"], len(queue) - 4)
        self.assertFalse(report["summary"]["coverage_complete"])
        safety = next(item for item in work_items if item["source_profile_id"] == "safetydata_v1")
        self.assertEqual(safety["next_action"], "implement_safetydata_document_profile")
        evidence_rows = [item for item in work_items if "evidence_ref" in item]
        self.assertEqual(len(evidence_rows), 4)
        try:
            from jsonschema import Draft202012Validator
        except ImportError:
            self.skipTest("jsonschema is an optional release-validation dependency")
        item_schema = json.loads((ROOT / "schemas/datapan.operation-document-work-item.v1.schema.json").read_text(encoding="utf-8"))
        validator = Draft202012Validator(item_schema)
        self.assertTrue(all(not list(validator.iter_errors(item)) for item in work_items))

    def test_capture_receipt_is_bound_to_private_raw_source_bytes(self) -> None:
        manifest = json.loads((ROOT / "reports/data-go-kr/operation-manifest.json").read_text(encoding="utf-8"))
        queue = DOCS.build_queue(manifest)
        identity = queue[0]["operation_identity"]
        with tempfile.TemporaryDirectory() as temp:
            capture_root = pathlib.Path(temp) / "raw"
            evidence_dir = pathlib.Path(temp) / "evidence"
            folder = capture_root / identity["dataset_id"] / identity["upstream_operation_key"]
            folder.mkdir(parents=True)
            for filename in ("catalogue.html", "operation-detail.html", "reference-guide.bin"):
                (folder / filename).write_bytes(filename.encode())
            source_times = {"catalogue": "2026-10-07T03:00:00Z", "operation_detail": "2026-10-07T03:01:00Z", "reference_guide": "2026-10-07T03:02:00Z"}
            DOCS._write_capture_receipt(folder, identity, status="acquired", reason_code=None, retrieved_at=source_times)
            self.assertEqual(DOCS._capture_retrieval_times(folder), source_times)
            state = DOCS._capture_work_status(identity, capture_root)
            self.assertEqual(state["status"], "acquired")
            report, work_items = DOCS.reconcile(queue, evidence_dir, capture_root)
            self.assertEqual(report["summary"]["statuses"]["acquired"], 1)
            self.assertEqual(next(item for item in work_items if item["operation_identity"]["operation_id"] == identity["operation_id"])["next_action"], "run_offline_document_parser")
            DOCS._write_capture_receipt(folder, identity, status="unsupported", reason_code="guide_format_unsupported_legacy_ole_document")
            state = DOCS._capture_work_status(identity, capture_root)
            self.assertEqual(state["next_action"], "implement_legacy_ole_or_hwp_guide_adapter")
            (folder / "reference-guide.bin").write_bytes(b"changed")
            state = DOCS._capture_work_status(identity, capture_root)
            self.assertEqual(state["status"], "invalid")
            self.assertEqual(state["reason_code"], "capture_receipt_document_binding_invalid")
            self.assertNotIn("reference_guide", DOCS._capture_retrieval_times(folder))

    def test_capture_root_must_be_dedicated_private_and_outside_repository(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = pathlib.Path(temp) / "private-captures"
            prepared = DOCS.prepare_private_capture_root(root)
            self.assertEqual(prepared.stat().st_mode & 0o777, 0o700)
            with self.assertRaisesRegex(DOCS.EvidenceError, "capture_root_symlink_rejected"):
                link = pathlib.Path(temp) / "capture-link"
                link.symlink_to(root)
                DOCS.prepare_private_capture_root(link)
        with self.assertRaisesRegex(DOCS.EvidenceError, "capture_root_not_private_dedicated_path"):
            DOCS.prepare_private_capture_root(ROOT / "reports" / "raw-captures")


if __name__ == "__main__":
    unittest.main()
