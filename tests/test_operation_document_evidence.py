from __future__ import annotations

import copy
import importlib.util
import io
import json
import pathlib
import socket
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

    def test_docx_expansion_limit_rejects_zip_bomb(self) -> None:
        bomb = io.BytesIO()
        with zipfile.ZipFile(bomb, "w", zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("word/document.xml", b" " * 10000)
        with mock.patch.object(DOCS, "MAX_DOCX_EXPANDED_BYTES", 100):
            with self.assertRaisesRegex(DOCS.EvidenceError, "docx_expansion_limit"):
                DOCS._docx_tables(bomb.getvalue())

    def test_source_allowlist_rejects_query_redirect_and_private_dns(self) -> None:
        with self.assertRaisesRegex(DOCS.EvidenceError, "outside_official_allowlist"):
            DOCS._validate_official_url("https://127.0.0.1/data/1/openapi.do", purpose="catalogue")
        with self.assertRaisesRegex(DOCS.EvidenceError, "catalogue_query_not_allowed"):
            DOCS._validate_official_url("https://www.data.go.kr/data/1/openapi.do?key=private", purpose="catalogue")
        private_dns = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 443))]
        with self.assertRaisesRegex(DOCS.EvidenceError, "private_or_reserved"):
            DOCS._public_ip_for("www.data.go.kr", resolver=lambda *_args, **_kwargs: private_dns)

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

    def test_full_registered_queue_keeps_safetydata_and_exclusions_separate(self) -> None:
        manifest = json.loads((ROOT / "reports/data-go-kr/operation-manifest.json").read_text(encoding="utf-8"))
        queue = DOCS.build_queue(manifest)
        self.assertEqual(len(queue), 12662)
        safetydata = [row for row in queue if row["operation_identity"]["source_system"] == "safetydata.go.kr"]
        self.assertEqual(len(safetydata), 180)
        self.assertEqual(safetydata[0]["source_routes"][0]["host"], "www.safetydata.go.kr")
        self.assertEqual(manifest["summary"]["exclusions"]["link_operations"], 8871)
        self.assertEqual(manifest["summary"]["exclusions"]["operationless_catalog_entries"], 473)


if __name__ == "__main__":
    unittest.main()
