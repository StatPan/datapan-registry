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


def _seoul_openapi_pages(*, sample_url: str | None = None) -> tuple[bytes, bytes]:
    sample_url = sample_url or "http://openapi.seoul.go.kr:8088/PRIVATE_KEY/json/SearchSTNBySubwayLineInfo/1/5/"
    request = _table([
        ["변수명", "타입", "변수설명", "값설명"],
        ["KEY", "STRING(필수)", "인증키", ""],
        ["TYPE", "STRING(필수)", "응답 형식", "xml, xmlf, xls, json"],
        ["SERVICE", "STRING(필수)", "서비스명", "SearchSTNBySubwayLineInfo"],
        ["START_INDEX", "INTEGER(필수)", "시작 위치", ""],
        ["END_INDEX", "INTEGER(필수)", "종료 위치", ""],
        ["STATION_CD", "STRING(선택)", "역 코드", ""],
        ["STATION_NM", "STRING(선택)", "역 이름", ""],
        ["LINE_NUM", "STRING(선택)", "호선", ""],
    ])
    sample = _table([["샘플 URL"], ["예시", sample_url]])
    output = _table([
        ["No", "출력명", "출력설명"],
        ["1", "list_total_count", "전체 건수"],
        ["2", "RESULT.CODE", "처리 결과 코드"],
        ["3", "RESULT.MESSAGE", "처리 결과 메시지"],
        ["4", "STATION_CD", "역 코드"],
    ])
    codes = _table([
        ["INFO-000", "정상 처리되었습니다."],
        ["INFO-200", "해당하는 데이터가 없습니다."],
        ["INFO-100", "인증키가 유효하지 않습니다."],
        ["ERROR-336", "데이터요청은 한번에 최대 1000건을 넘지 않도록 수정하세요."],
        ["ERROR-500", "일반 오류입니다."],
        ["ERROR-601", "SQL 문장 오류 입니다."],
    ])
    openapi_raw = (request + sample + output + codes).encode("utf-8")
    dataset_raw = (
        "<h1>서울교통공사_노선별 지하철역 정보</h1>"
        "<p>서울교통공사에서 제공하는 1~8호선, 9호선 2~3단계(언주~중앙보훈병원) 노선별 지하철역을 제공하는 서비스 입니다.</p>"
        "<p>해당 데이터는 종료된 서비스입니다.</p>"
    ).encode("utf-8")
    return openapi_raw, dataset_raw


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
        schema = json.loads((ROOT / "schemas/datapan.operation-document-evidence.v2.schema.json").read_text(encoding="utf-8"))
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

    def test_legacy_v1_sidecars_and_distinct_denominator_kind_remain_release_valid(self) -> None:
        try:
            from jsonschema import Draft202012Validator
        except ImportError:
            self.skipTest("jsonschema is an optional release-validation dependency")

        legacy_schema_path = ROOT / "schemas/datapan.operation-document-evidence.v1.schema.json"
        legacy_schema_bytes = legacy_schema_path.read_bytes()
        self.assertEqual(
            hashlib.sha256(legacy_schema_bytes).hexdigest(),
            "0b4a5a7ab10eeccb523d2af8a8e62e76f14a6243eea00558ac49e9959e7a3d1d",
        )
        legacy_validator = Draft202012Validator(json.loads(legacy_schema_bytes))
        for path in (
            ROOT / "reports/operation-document-evidence/15001697-24807.json",
            ROOT / "reports/operation-document-evidence/15001808-16811.json",
        ):
            self.assertEqual(list(legacy_validator.iter_errors(json.loads(path.read_text(encoding="utf-8")))), [])

        manifest_schema = json.loads((ROOT / "schemas/datapan.release-manifest.v1.schema.json").read_text(encoding="utf-8"))
        manifest = json.loads((ROOT / "manifest.json").read_text(encoding="utf-8"))
        Draft202012Validator(manifest_schema).validate(manifest)
        allowed_kinds = manifest_schema["$defs"]["artifact"]["properties"]["kind"]["enum"]
        self.assertIn("operation_denominator", allowed_kinds)
        denominator = next(item for item in manifest["artifacts"] if item["path"] == "reports/kosis/operation-denominator.json")
        expectation = next(item for item in manifest["artifacts"] if item["path"] == "policy/data-go-kr-operation-denominator-expectation.json")
        self.assertEqual(denominator["kind"], "operation_denominator")
        self.assertNotEqual(denominator["schema"], expectation["schema"])

    def test_kosis_current_operation_sidecar_binds_fixed_selector_without_inventing_http_method(self) -> None:
        try:
            from jsonschema import Draft202012Validator
        except ImportError:
            self.skipTest("jsonschema is an optional release-validation dependency")
        artifact = ROOT / "reports/operation-document-evidence/source-scopes/kosis-statistics-data-dt-1b41.json"
        evidence = json.loads(artifact.read_text(encoding="utf-8"))
        schema = json.loads((ROOT / "schemas/datapan.operation-document-evidence.v2.schema.json").read_text(encoding="utf-8"))
        self.assertEqual(list(Draft202012Validator(schema).iter_errors(evidence)), [])
        self.assertEqual(evidence["identity"]["source_id"], "kosis")
        self.assertEqual(evidence["identity"]["operation_id"], "kosis-statistics-data-dt-1b41")
        self.assertEqual(evidence["operation_document"]["title"]["value"], "통계표선택 방법")
        self.assertEqual(evidence["operation_document"]["purpose"]["status"], "not_found_in_parsed_operation_sources")
        self.assertEqual(evidence["transport"]["path"]["value"], "/openapi/Param/statisticsParameterData.do")
        self.assertIsNone(evidence["transport"]["http_method"]["value"])
        self.assertEqual(evidence["transport"]["fixed_query_selectors"][0]["name"], "method")
        self.assertEqual(evidence["transport"]["fixed_query_selectors"][0]["value"], "getList")
        self.assertEqual(evidence["authentication"]["parameter_names"], ["apiKey"])
        self.assertEqual(evidence["authentication"]["placement"], "query")
        self.assertEqual(evidence["response_contract"]["documented_error_contract"]["format"], "xml")
        self.assertIn("DT", {field["name"] for field in evidence["response_contract"]["declared_output_fields"]})
        self.assertEqual(evidence["response_contract"]["success_branches"], [])
        source_ids = {binding["source_id"] for binding in evidence["source_bindings"]}
        refs = []
        def visit(value):
            if isinstance(value, dict):
                if isinstance(value.get("locator"), dict):
                    refs.append(value)
                for child in value.values():
                    visit(child)
            elif isinstance(value, list):
                for child in value:
                    visit(child)
        visit(evidence)
        self.assertTrue(all(ref["locator"]["source_id"] in source_ids for ref in refs))
        self.assertNotIn("https://kosis.kr/openapi/Param/", artifact.read_text(encoding="utf-8"))

    def test_seoul_registered_operation_preserves_official_port_and_termination_without_inventing_method(self) -> None:
        try:
            from jsonschema import Draft202012Validator
        except ImportError:
            self.skipTest("jsonschema is an optional release-validation dependency")
        openapi_raw, dataset_raw = _seoul_openapi_pages()
        operation = {
            "candidate_id": "seoul-open-data-subway-station-list",
            "endpoint_template": "http://openapi.seoul.go.kr:8088/{KEY}/{format}/{service}/{start_index}/{end_index}",
        }
        evidence = DOCS.parse_seoul_openapi_evidence(
            operation,
            openapi_raw=openapi_raw,
            dataset_raw=dataset_raw,
            openapi_retrieved_at="2026-10-07T06:23:10Z",
            dataset_retrieved_at="2026-10-07T06:18:44Z",
        )
        schema = json.loads((ROOT / "schemas/datapan.operation-document-evidence.v2.schema.json").read_text(encoding="utf-8"))
        validator = Draft202012Validator(schema)
        self.assertEqual(list(validator.iter_errors(evidence)), [])
        self.assertEqual(evidence["identity"]["source_id"], "seoul_open_data")
        self.assertEqual(evidence["identity"]["operation_id"], "seoul-open-data-subway-station-list")
        self.assertEqual(evidence["operation_document"]["title"]["value"], "서울교통공사_노선별 지하철역 정보")
        self.assertTrue(evidence["operation_document"]["purpose"]["value"].startswith("서울교통공사에서 제공하는 "))
        status = evidence["operation_document"]["service_status"]
        self.assertEqual((status["classification"], status["status"]), ("terminated", "documented"))
        self.assertEqual(evidence["transport"]["scheme"]["value"], "http")
        self.assertEqual(evidence["transport"]["host"]["value"], "openapi.seoul.go.kr")
        self.assertEqual(evidence["transport"]["port"], 8088)
        self.assertTrue(evidence["transport"]["port_source_refs"])
        self.assertIsNone(evidence["transport"]["http_method"]["value"])
        self.assertEqual(evidence["transport"]["http_method"]["status"], "unknown")
        params = {row["name"]: row for row in evidence["parameters"]}
        self.assertEqual(params["KEY"]["requiredness"]["value"], "required")
        self.assertEqual(params["KEY"]["location"]["value"], "path")
        self.assertEqual(params["STATION_CD"]["requiredness"]["value"], "optional")
        self.assertEqual(evidence["response_assertion"]["empty_result_semantics"]["status"], "unknown")
        result_codes = evidence["response_contract"]["documented_error_contract"]["codes"]
        classes = {code["value"]: code["classification"] for code in result_codes}
        self.assertEqual(classes["INFO-200"], "empty_result")
        self.assertEqual(classes["INFO-100"], "credential")
        self.assertEqual(classes["ERROR-336"], "size_limit")
        self.assertEqual(classes["ERROR-500"], "unknown")
        self.assertEqual(classes["ERROR-601"], "unknown")
        error_values = evidence["response_contract"]["provider_result_codes"]["error_values"]["values"]
        self.assertEqual(set(error_values), {"ERROR-336", "ERROR-500", "ERROR-601"})
        self.assertNotIn("INFO-200", error_values)

        encoded = json.dumps(evidence, ensure_ascii=False)
        for private in ("PRIVATE_KEY", "http://openapi.seoul.go.kr", "list_total_count\":\"", "sample_url"):
            self.assertNotIn(private, encoded)

        for invalid_port in (0, 65536):
            broken = copy.deepcopy(evidence)
            broken["transport"]["port"] = invalid_port
            self.assertTrue(list(validator.iter_errors(broken)))
        broken = copy.deepcopy(evidence)
        broken["transport"].pop("port_source_refs")
        self.assertTrue(list(validator.iter_errors(broken)))

    def test_seoul_result_classes_require_message_semantics_not_code_prefixes(self) -> None:
        self.assertEqual(DOCS._classify_seoul_result_code("ERROR-500", "일반 오류입니다."), "unknown")
        self.assertEqual(DOCS._classify_seoul_result_code("ERROR-601", "SQL 문장 오류 입니다."), "unknown")
        self.assertEqual(DOCS._classify_seoul_result_code("ERROR-500", "오류 내용을 수정하세요."), "unknown")
        self.assertEqual(DOCS._classify_seoul_result_code("ERROR-500", "서버 오류입니다."), "server")
        self.assertEqual(DOCS._classify_seoul_result_code("ERROR-336", "최대 1000건을 넘지 않도록 수정하세요."), "size_limit")
        self.assertEqual(DOCS._classify_seoul_result_code("INFO-100", "인증키가 유효하지 않습니다."), "credential")

    def test_seoul_path_sample_rejects_userinfo_fragment_and_query(self) -> None:
        operation = {
            "candidate_id": "seoul-open-data-subway-station-list",
            "endpoint_template": "http://openapi.seoul.go.kr:8088/{KEY}/{format}/{service}/{start_index}/{end_index}",
        }
        invalid_urls = (
            "http://credential@openapi.seoul.go.kr:8088/KEY/json/SearchSTNBySubwayLineInfo/1/5/",
            "http://openapi.seoul.go.kr:8088/KEY/json/SearchSTNBySubwayLineInfo/1/5/#fragment",
            "http://openapi.seoul.go.kr:8088/KEY/json/SearchSTNBySubwayLineInfo/1/5/?sample=value",
        )
        for sample_url in invalid_urls:
            with self.subTest(sample_url_kind=sample_url.split(":", 1)[0]):
                openapi_raw, dataset_raw = _seoul_openapi_pages(sample_url=sample_url)
                with self.assertRaisesRegex(DOCS.EvidenceError, "seoul_operation_path_example_endpoint_mismatch"):
                    DOCS.parse_seoul_openapi_evidence(
                        operation,
                        openapi_raw=openapi_raw,
                        dataset_raw=dataset_raw,
                        openapi_retrieved_at="2026-10-07T06:23:10Z",
                        dataset_retrieved_at="2026-10-07T06:18:44Z",
                    )

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

    def test_openapi_operation_title_and_plain_purpose_are_source_bound_and_sensitive_text_is_omitted(self) -> None:
        document = _swagger_document()
        operation_spec = document["paths"]["/health"]["get"]
        operation_spec["description"] = "공공 헬스체크 상태를 조회합니다."
        evidence = DOCS.parse_openapi_evidence(
            _operation(), page_raw=_swagger_page(document), page_retrieved_at="2026-10-07T03:00:00Z",
        )
        operation_document = evidence["operation_document"]
        self.assertEqual(operation_document["title"]["value"], "헬스체크 조회")
        self.assertEqual(operation_document["title"]["status"], "documented")
        self.assertEqual(operation_document["purpose"]["value"], "공공 헬스체크 상태를 조회합니다.")
        self.assertEqual(operation_document["purpose"]["status"], "documented")
        pointers = {ref["locator"]["json_pointer"] for fact in operation_document.values() for ref in fact["source_refs"]}
        self.assertIn("#/paths/~1health/get/summary", pointers)
        self.assertIn("#/paths/~1health/get/description", pointers)
        self.assertEqual(evidence["effect"]["status"], "unknown")

        operation_spec["description"] = "응답 예시: {\"serviceKey\":\"PRIVATE_SOURCE_VALUE\"}"
        redacted = DOCS.parse_openapi_evidence(
            _operation(), page_raw=_swagger_page(document), page_retrieved_at="2026-10-07T03:00:00Z",
        )
        encoded = json.dumps(redacted, ensure_ascii=False)
        self.assertNotIn("PRIVATE_SOURCE_VALUE", encoded)
        self.assertEqual(redacted["operation_document"]["purpose"]["value"], None)
        self.assertEqual(redacted["operation_document"]["purpose"]["status"], "unknown")
        self.assertEqual(redacted["operation_document"]["purpose"]["source_refs"][0]["locator"]["json_pointer"], "#/paths/~1health/get/description")

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
        self.assertEqual(contract["payload"]["kind"], "json")
        self.assertEqual(contract["payload"]["media_types"], ["application/json"])
        self.assertEqual(contract["schema_shape"]["status"], "incomplete")
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
        self.assertEqual(contract["coded_result_field_inventory"]["status"], "incomplete")
        self.assertEqual(contract["coded_result_field_inventory"]["candidates"][0]["classification"], "recognized_result_code_name")
        fields = {item["path"]["value"]: item for item in contract["documented_fields"] if item["path"] and item["path"]["kind"] == "json_pointer"}
        self.assertEqual(fields["#/header/resultCode"]["value_type"], "string")
        self.assertEqual(contract["required_fields"], [])

    def test_success_response_branches_preserve_distinct_status_schemas_and_fields(self) -> None:
        document = _swagger_document()
        document["paths"]["/health"]["get"]["responses"]["201"] = {
            "description": "Created with status envelope",
            "schema": {
                "type": "object",
                "additionalProperties": False,
                "required": ["status"],
                "properties": {"status": {"type": "string"}},
            },
        }
        evidence = DOCS.parse_openapi_evidence(
            _operation(),
            page_raw=_swagger_page(document),
            page_retrieved_at="2026-10-07T03:00:00Z",
        )
        contract = evidence["response_contract"]
        self.assertEqual(contract["accepted_http_status_codes"]["values"], [200, 201])
        branches = {item["http_status_code"]: item for item in contract["success_branches"]}
        self.assertEqual(branches[200]["schema_source"]["json_pointer"], "#/paths/~1health/get/responses/200/schema")
        self.assertEqual(branches[200]["schema_shape"]["status"], "incomplete")
        self.assertEqual(branches[200]["root_shape"], {"status": "documented", "kind": "object", "qname": None, "source_refs": branches[200]["root_shape"]["source_refs"]})
        self.assertEqual(branches[201]["schema_source"]["json_pointer"], "#/paths/~1health/get/responses/201/schema")
        self.assertEqual(branches[201]["schema_shape"]["status"], "complete")
        self.assertEqual(branches[201]["root_shape"]["kind"], "object")
        fields_by_status = {(item["http_status_code"], item["name"]): item for item in contract["documented_fields"]}
        self.assertIn((200, "resultCode"), fields_by_status)
        self.assertIn((201, "status"), fields_by_status)
        candidates = contract["coded_result_field_inventory"]["candidates"]
        self.assertEqual([(item["http_status_code"], item["name"]) for item in candidates], [(200, "resultCode"), (201, "status")])
        self.assertEqual([item["name"] for item in branches[200]["coded_result_field_inventory"]["candidates"]], ["resultCode"])
        self.assertEqual(branches[200]["provider_result_codes"]["status"], "unknown")
        self.assertEqual(branches[200]["result_collection"]["path"], {"kind": "json_pointer", "value": "#/body/items"})
        self.assertEqual(branches[201]["result_collection"]["status"], "unknown")
        self.assertEqual(contract["schema_shape"]["status"], "incomplete")

    def test_numeric_http_error_response_branches_preserve_status_and_union_member_scope(self) -> None:
        document = _swagger_document()
        document["paths"]["/health"]["get"]["responses"]["400"] = {
            "description": "Bad request",
            "schema": {
                "oneOf": [
                    {
                        "type": "object",
                        "additionalProperties": False,
                        "required": ["code"],
                        "properties": {"code": {"type": "string"}},
                    },
                    {"type": "string"},
                ],
            },
        }
        document["paths"]["/health"]["get"]["responses"]["503"] = {
            "description": "Service unavailable",
            "schema": {"type": "array", "items": {"type": "string"}},
        }
        evidence = DOCS.parse_openapi_evidence(
            _operation(),
            page_raw=_swagger_page(document),
            page_retrieved_at="2026-10-07T03:00:00Z",
        )
        contract = evidence["response_contract"]
        self.assertEqual(contract["accepted_http_status_codes"]["values"], [200])
        branches = contract["documented_http_error_branches"]
        self.assertEqual(
            [(item["http_status_code"], item["schema_variant"]) for item in branches],
            [(400, {"kind": "oneOf", "index": 0}), (400, {"kind": "oneOf", "index": 1}), (503, {"kind": "single", "index": None})],
        )
        self.assertEqual(branches[0]["schema_source"]["json_pointer"], "#/paths/~1health/get/responses/400/schema/oneOf/0")
        self.assertEqual(branches[0]["root_shape"]["kind"], "object")
        self.assertEqual(branches[1]["root_shape"]["kind"], "scalar")
        self.assertEqual(branches[2]["root_shape"]["kind"], "array")
        status_refs = [item["locator"]["json_pointer"] for branch in branches for item in branch["source_refs"] if item["evidence_kind"] == "documented_http_error_status"]
        self.assertEqual(status_refs, ["#/paths/~1health/get/responses/400", "#/paths/~1health/get/responses/400", "#/paths/~1health/get/responses/503"])
        try:
            from jsonschema import Draft202012Validator
        except ImportError:
            return
        schema = json.loads((ROOT / "schemas/datapan.operation-document-evidence.v2.schema.json").read_text(encoding="utf-8"))
        self.assertEqual(list(Draft202012Validator(schema).iter_errors(evidence)), [])

    def test_explicitly_unnamespaced_xml_response_qnames_are_preserved(self) -> None:
        document = _swagger_document()
        document["produces"] = ["application/xml"]
        document["paths"]["/health"]["get"]["responses"]["200"]["schema"] = {
            "type": "object",
            "additionalProperties": False,
            "xml": {"name": "Response", "namespace": ""},
            "required": ["items"],
            "properties": {
                "items": {
                    "type": "array",
                    "xml": {"name": "items", "namespace": "", "wrapped": True},
                    "items": {
                        "type": "object",
                        "additionalProperties": False,
                        "xml": {"name": "item", "namespace": ""},
                        "properties": {"name": {"type": "string", "xml": {"name": "name", "namespace": ""}}},
                    },
                },
            },
        }
        evidence = DOCS.parse_openapi_evidence(
            _operation(), page_raw=_swagger_page(document), page_retrieved_at="2026-10-07T03:00:00Z",
        )
        branch = evidence["response_contract"]["success_branches"][0]
        self.assertEqual(branch["root_shape"]["status"], "documented")
        self.assertEqual(branch["root_shape"]["qname"], {"namespace": "", "local_name": "Response"})
        self.assertEqual(branch["result_collection"]["status"], "documented")
        self.assertEqual(branch["result_collection"]["container_path"]["segments"][0]["namespace"], "")
        self.assertEqual(branch["result_collection"]["item_path"]["segments"][0]["namespace"], "")

        document["paths"]["/health"]["get"]["responses"]["200"]["schema"]["xml"] = {"name": "Response"}
        namespace_unknown = DOCS.parse_openapi_evidence(
            _operation(), page_raw=_swagger_page(document), page_retrieved_at="2026-10-07T03:00:00Z",
        )["response_contract"]["success_branches"][0]
        self.assertEqual(namespace_unknown["root_shape"]["status"], "unknown")
        self.assertEqual(namespace_unknown["root_shape"]["kind"], "xml_element")
        self.assertEqual(namespace_unknown["root_shape"]["qname"], {"namespace": None, "local_name": "Response"})
        self.assertEqual(namespace_unknown["result_collection"]["status"], "unknown")
        try:
            from jsonschema import Draft202012Validator
        except ImportError:
            return
        schema = json.loads((ROOT / "schemas/datapan.operation-document-evidence.v2.schema.json").read_text(encoding="utf-8"))
        self.assertEqual(list(Draft202012Validator(schema).iter_errors(evidence)), [])

    def test_openapi_union_members_remain_disjoint_source_bound_response_branches(self) -> None:
        document = _swagger_document()
        document["paths"]["/health"]["get"]["responses"]["200"]["schema"] = {
            "oneOf": [
                {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["data"],
                    "properties": {"data": {"type": "array", "items": {"type": "string"}}},
                },
                {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["error"],
                    "properties": {"error": {"type": "string"}},
                },
            ]
        }
        evidence = DOCS.parse_openapi_evidence(
            _operation(),
            page_raw=_swagger_page(document),
            page_retrieved_at="2026-10-07T03:00:00Z",
        )
        contract = evidence["response_contract"]
        branches = contract["success_branches"]
        self.assertEqual([(item["http_status_code"], item["schema_variant"]) for item in branches], [(200, {"kind": "oneOf", "index": 0}), (200, {"kind": "oneOf", "index": 1})])
        self.assertEqual([item["schema_source"]["json_pointer"] for item in branches], [
            "#/paths/~1health/get/responses/200/schema/oneOf/0",
            "#/paths/~1health/get/responses/200/schema/oneOf/1",
        ])
        self.assertEqual(contract["schema_shape"]["status"], "complete")
        self.assertEqual(branches[0]["root_shape"]["kind"], "object")
        self.assertEqual(branches[0]["result_collection"]["path"], {"kind": "json_pointer", "value": "#/data"})
        self.assertEqual(branches[1]["result_collection"]["status"], "unknown")
        self.assertEqual([candidate["name"] for candidate in branches[1]["coded_result_field_inventory"]["candidates"]], ["error"])
        self.assertEqual(branches[0]["coded_result_field_inventory"]["candidates"], [])
        try:
            from jsonschema import Draft202012Validator
        except ImportError:
            return
        schema = json.loads((ROOT / "schemas/datapan.operation-document-evidence.v2.schema.json").read_text(encoding="utf-8"))
        self.assertEqual(list(Draft202012Validator(schema).iter_errors(evidence)), [])

    def test_anyof_and_unbounded_unions_preserve_variant_scope_or_fail_closed(self) -> None:
        document = _swagger_document()
        document["paths"]["/health"]["get"]["responses"]["200"]["schema"] = {
            "anyOf": [
                {"type": "array", "items": {"type": "string"}},
                {"type": "string"},
            ]
        }
        anyof = DOCS.parse_openapi_evidence(
            _operation(), page_raw=_swagger_page(document), page_retrieved_at="2026-10-07T03:00:00Z",
        )["response_contract"]
        self.assertEqual([branch["schema_variant"]["kind"] for branch in anyof["success_branches"]], ["anyOf", "anyOf"])
        self.assertEqual(anyof["schema_shape"]["status"], "complete")

        document["paths"]["/health"]["get"]["responses"]["200"]["schema"] = {"oneOf": []}
        unsupported = DOCS.parse_openapi_evidence(
            _operation(), page_raw=_swagger_page(document), page_retrieved_at="2026-10-07T03:00:00Z",
        )["response_contract"]["success_branches"][0]
        self.assertEqual(unsupported["schema_variant"], {"kind": "unsupported_union", "index": None})
        self.assertEqual(unsupported["schema_shape"]["status"], "incomplete")
        self.assertEqual(unsupported["root_shape"]["kind"], "unknown")
        self.assertEqual(unsupported["documented_fields"], [])

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

    def test_closed_schema_records_ambiguous_status_without_marking_it_code_free(self) -> None:
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
        self.assertEqual(contract["status"], "unknown")
        self.assertEqual(contract["evidence_strength"], "unknown")
        inventory = evidence["response_contract"]["coded_result_field_inventory"]
        self.assertEqual(inventory["status"], "complete")
        self.assertEqual(inventory["candidates"][0]["name"], "status")
        self.assertEqual(inventory["candidates"][0]["classification"], "ambiguous_code_semantics")
        self.assertEqual(evidence["response_contract"]["schema_shape"]["status"], "complete")

    def test_closed_code_free_shape_is_a_policy_input_without_auto_n_a(self) -> None:
        document = _swagger_document()
        document["paths"]["/health"]["get"]["responses"]["200"]["schema"] = {
            "type": "object", "additionalProperties": False,
            "properties": {"items": {
                "type": "array", "items": {
                    "type": "object", "additionalProperties": False,
                    "properties": {"name": {"type": "string"}},
                },
            }},
        }
        evidence = DOCS.parse_openapi_evidence(
            _operation(),
            page_raw=_swagger_page(document),
            page_retrieved_at="2026-10-07T03:00:00Z",
        )
        response_contract = evidence["response_contract"]
        self.assertEqual(response_contract["provider_result_codes"]["status"], "unknown")
        self.assertEqual(response_contract["schema_shape"]["status"], "complete")
        self.assertEqual(response_contract["coded_result_field_inventory"]["status"], "complete")
        self.assertEqual(response_contract["coded_result_field_inventory"]["candidates"], [])
        self.assertTrue(response_contract["schema_shape"]["source_refs"])
        self.assertEqual(response_contract["accepted_http_status_codes"]["values"], [200])

    def test_json_string_response_example_is_not_promoted_without_nested_locator(self) -> None:
        document = _swagger_document()
        response = document["paths"]["/health"]["get"]["responses"]["200"]
        response["description"] = "Successful response"
        response["examples"] = {"application/json": json.dumps({"header": {"resultCode": "00"}})}
        evidence = DOCS.parse_openapi_evidence(
            _operation(),
            page_raw=_swagger_page(document),
            page_retrieved_at="2026-10-07T03:00:00Z",
        )
        contract = evidence["response_contract"]["provider_result_codes"]
        self.assertEqual(contract["status"], "unknown")
        self.assertEqual(contract["success_values"]["values"], [])
        self.assertNotIn("provider_result_code_success_official_example_value", [ref["evidence_kind"] for ref in contract["source_refs"]])

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

    def test_transient_source_failures_remain_retryable_not_missing(self) -> None:
        self.assertEqual(DOCS._capture_document_status("source_deadline_exceeded"), ("retryable", "retry_official_document_capture"))
        self.assertEqual(DOCS._capture_document_status("source_transport_failed"), ("retryable", "retry_official_document_capture"))
        self.assertEqual(DOCS._capture_document_status("official_document_not_found"), ("missing", "locate_official_document"))
        identity = DOCS._safe_operation_identity(_operation())
        with tempfile.TemporaryDirectory() as temp:
            capture_root = pathlib.Path(temp) / "captures"
            folder = capture_root / identity["dataset_id"] / identity["upstream_operation_key"]
            folder.mkdir(parents=True)
            DOCS._write_capture_receipt(folder, identity, status="retryable", reason_code="source_deadline_exceeded")
            state = DOCS._capture_work_status(identity, capture_root)
            self.assertEqual(state, {"status": "retryable", "next_action": "retry_official_document_capture", "reason_code": "source_deadline_exceeded"})

    def test_document_request_pacer_and_private_root_disk_cap_are_bounded(self) -> None:
        pacer = DOCS._OfficialDocumentRequestPacer(1.0)
        with mock.patch.object(DOCS.time, "monotonic", side_effect=[0.0, 0.0, 0.2, 1.0]), mock.patch.object(DOCS.time, "sleep") as sleep:
            pacer.wait()
            pacer.wait()
        sleep.assert_called_once_with(0.8)
        with tempfile.TemporaryDirectory() as temp:
            root = pathlib.Path(temp) / "captures"
            root.mkdir()
            with mock.patch.object(DOCS, "MAX_PRIVATE_CAPTURE_ROOT_BYTES", 4):
                with self.assertRaisesRegex(DOCS.EvidenceError, "source_capture_disk_budget_exceeded"):
                    DOCS._write_captured_document(root, root / "catalogue.html", b"five bytes")

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
            schema = json.loads((ROOT / "schemas/datapan.operation-document-capture-receipt.v2.schema.json").read_text())
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
        report, work_items = DOCS.reconcile(queue, DOCS.EVIDENCE_V2_DIR)
        self.assertEqual(sum(report["summary"]["statuses"].values()), len(queue))
        v2_sidecars = list((ROOT / "reports/operation-document-evidence/v2").glob("*.json"))
        self.assertEqual(report["summary"]["statuses"]["parsed_with_unknowns"], len(v2_sidecars))
        self.assertEqual(report["summary"]["statuses"]["pending"], len(queue) - len(v2_sidecars))
        self.assertFalse(report["summary"]["coverage_complete"])
        safety = next(item for item in work_items if item["source_profile_id"] == "safetydata_v1")
        self.assertEqual(safety["next_action"], "implement_safetydata_document_profile")
        evidence_rows = [item for item in work_items if "evidence_ref" in item]
        self.assertEqual(len(evidence_rows), len(v2_sidecars))
        try:
            from jsonschema import Draft202012Validator
        except ImportError:
            self.skipTest("jsonschema is an optional release-validation dependency")
        item_schema = json.loads((ROOT / "schemas/datapan.operation-document-work-item.v2.schema.json").read_text(encoding="utf-8"))
        validator = Draft202012Validator(item_schema)
        self.assertTrue(all(not list(validator.iter_errors(item)) for item in work_items))
        reconciliation_schema = json.loads((ROOT / "schemas/datapan.operation-document-reconciliation.v2.schema.json").read_text(encoding="utf-8"))
        self.assertEqual(list(Draft202012Validator(reconciliation_schema).iter_errors(report)), [])

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
