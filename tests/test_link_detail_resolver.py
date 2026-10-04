from __future__ import annotations

import hashlib
import importlib.util
import io
import json
import os
import pathlib
import copy
import tempfile
import unittest
import urllib.request
import urllib.response
from email.message import Message
from datetime import datetime, timedelta, timezone
from unittest import mock


ROOT = pathlib.Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "process-upstream-catalogue-candidate.py"
FIXTURE = ROOT / "tests" / "fixtures" / "link_detail_resolver" / "manual-native-samples.json"
SPEC = importlib.util.spec_from_file_location("process_upstream_catalogue_candidate_link_resolver", SCRIPT)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class CurrentTemplateParserTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.fixture = json.loads(FIXTURE.read_text(encoding="utf-8"))
        cls.samples = {row["dataset_id"]: row for row in cls.fixture["samples"]}

    def test_fixture_is_explicitly_manual_and_resolver_bodies_retain_native_hashes(self) -> None:
        self.assertTrue(self.fixture["classification"]["manual"])
        self.assertFalse(self.fixture["classification"]["operational"])
        self.assertFalse(self.fixture["classification"]["historical_b_evidence"])
        self.assertFalse(self.fixture["classification"]["new_a_observation"])
        self.assertFalse(self.fixture["classification"]["publication_evidence"])
        self.assertEqual(self.fixture["capture_policy"]["physical_requests_per_identity"], 3)
        for sample in self.samples.values():
            page = sample["detail_page"]
            resolver = sample["resolver"]
            self.assertEqual(page["effective_url"], page["requested_url"])
            self.assertEqual(page["http_status"], 200)
            self.assertEqual(len(page["full_response_sha256"]), 64)
            body = resolver["response_body"].encode("utf-8")
            self.assertEqual(len(body), resolver["response_size_bytes"])
            self.assertEqual(hashlib.sha256(body).hexdigest(), resolver["response_sha256"])
            self.assertEqual(resolver["effective_url"], resolver["requested_url"])
            self.assertEqual(resolver["http_status"], 200)
            self.assertEqual(resolver["method"], "GET")
            self.assertEqual(resolver["historical_operation_count"], 3)
            self.assertEqual(resolver["resolved_url_matches_historical_operation_count"], 1)

    def test_native_current_template_samples_bind_button_and_hidden_ids(self) -> None:
        for dataset_id, sample in self.samples.items():
            with self.subTest(dataset_id=dataset_id):
                parsed = MODULE.extract_current_template_dataset_id(
                    sample["detail_page"]["sanitized_html_excerpt"], dataset_id,
                )
                self.assertEqual(parsed, {
                    "dataset_id": dataset_id,
                    "public_data_pk": dataset_id,
                    "public_data_detail_pk": json.loads(sample["resolver"]["response_body"])["publicDataDetailPk"],
                })

    def test_wrong_button_dataset_and_conflicting_hidden_ids_fail_closed(self) -> None:
        sample = self.samples["15056854"]["detail_page"]["sanitized_html_excerpt"]
        wrong_button = sample.replace("fn_goUrlLink('15056854')", "fn_goUrlLink('15056855')")
        wrong_hidden_id = sample.replace('id="publicDataPk" value="15056854"', 'id="publicDataPk" value="15056855"')
        conflicting_hidden_id = sample + '\n<input type="hidden" id="publicDataPk" value="15056855"/>'
        invalid_cases = (
            (wrong_button, "current_template_shortcut_id_mismatch"),
            (wrong_hidden_id, "current_template_public_data_pk_mismatch"),
            (conflicting_hidden_id, "current_template_public_data_pk_ambiguous"),
        )
        for html, code in invalid_cases:
            with self.subTest(code=code), self.assertRaises(ValueError) as caught:
                MODULE.extract_current_template_dataset_id(html, "15056854")
            self.assertEqual(str(caught.exception), code)

        conflicting_detail_pk = sample + (
            '\n<input type="hidden" id="publicDataDetailPk" value="uddi:conflicting-page-value"/>'
        )
        with self.assertRaises(ValueError) as caught:
            MODULE.extract_current_template_dataset_id(conflicting_detail_pk, "15056854")
        self.assertEqual(str(caught.exception), "current_template_detail_pk_ambiguous")

    def test_unrelated_script_or_anchor_does_not_stand_in_for_the_record_button(self) -> None:
        sample = self.samples["15056854"]["detail_page"]["sanitized_html_excerpt"]
        unrelated = (
            "<script>fn_goUrlLink('15056854')</script>"
            '<a href="https://api.example.test/open" onclick="fn_LinkApiRequest()">API</a>'
        )
        self.assertIsNone(MODULE.extract_current_template_dataset_id(unrelated, "15056854"))
        self.assertIsNone(MODULE.extract_current_template_dataset_id(
            '<script>fn_goUrlLink(\'15056854\')</script>', "15056854",
        ))
        legacy_anchor = '<a href="https://api.example.test/open" onclick="fn_LinkApiRequest()">API</a>'
        self.assertIsNone(MODULE.extract_current_template_dataset_id(legacy_anchor, "15056854"))
        self.assertEqual(
            MODULE.DETAIL_HELPERS.extract_link_detail_operation_urls(legacy_anchor),
            ["https://api.example.test/open"],
        )
        self.assertIn("fn_goUrlLink('15056854')", sample)


class LinkResolverPureValidationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.fixture = json.loads(FIXTURE.read_text(encoding="utf-8"))
        cls.samples = {row["dataset_id"]: row for row in cls.fixture["samples"]}

    def validate(self, sample: dict, raw: bytes | None = None) -> dict:
        page = MODULE.extract_current_template_dataset_id(
            sample["detail_page"]["sanitized_html_excerpt"], sample["dataset_id"],
        )
        assert page is not None
        body = raw if raw is not None else sample["resolver"]["response_body"].encode("utf-8")
        return MODULE.validate_link_resolver_response(
            body,
            sample["dataset_id"],
            sample["resolver"]["observed_at"],
            expected_public_data_detail_pk=page["public_data_detail_pk"],
        )

    def assert_rejected(self, sample: dict, body: bytes, code: str) -> None:
        with self.assertRaises(ValueError) as caught:
            self.validate(sample, body)
        self.assertEqual(str(caught.exception), code)

    def test_native_resolver_metadata_is_bound_to_page_uddi_without_numeric_echo_requirement(self) -> None:
        for sample in self.samples.values():
            with self.subTest(dataset_id=sample["dataset_id"]):
                parsed = self.validate(sample)
                payload = json.loads(sample["resolver"]["response_body"])
                page = MODULE.extract_current_template_dataset_id(
                    sample["detail_page"]["sanitized_html_excerpt"], sample["dataset_id"],
                )
                self.assertEqual(parsed["link_url"], payload["linkUrl"])
                self.assertEqual(parsed["link_url_sha256"], hashlib.sha256(payload["linkUrl"].encode()).hexdigest())
                self.assertEqual(parsed["response_sha256"], sample["resolver"]["response_sha256"])
                self.assertEqual(parsed["response_bytes"], sample["resolver"]["response_size_bytes"])
                self.assertEqual(parsed["observed_at"], sample["resolver"]["observed_at"])
                self.assertEqual(parsed["public_data_detail_pk"], page["public_data_detail_pk"])
                self.assertIsNone(parsed["public_data_pk"])

    def test_detail_echo_is_bound_to_page_uddi_but_is_not_required_when_absent(self) -> None:
        sample = self.samples["15056854"]
        mismatched = sample["resolver"]["response_body"].replace(
            "uddi:1ca4ede2-911d-486a-b374-e7cf8650d25c_201712281924",
            "uddi:wrong-page-value",
            1,
        ).encode("utf-8")
        self.assert_rejected(sample, mismatched, "link_resolver_detail_echo_mismatch")
        omitted_echo = (
            '{"status":true,"linkUrl":"http://data.seoul.go.kr/dataList/datasetView.do?infId=OA-109"}'
        ).encode("utf-8")
        accepted = self.validate(sample, omitted_echo)
        self.assertIsNone(accepted["public_data_detail_pk"])

    def test_echo_cannot_bind_without_a_page_uddi_but_missing_echo_stays_optional(self) -> None:
        sample = self.samples["15056854"]
        page_html = sample["detail_page"]["sanitized_html_excerpt"]
        page_html = page_html.replace(
            '<input type="hidden" id="publicDataDetailPk" value="uddi:1ca4ede2-911d-486a-b374-e7cf8650d25c_201712281924"/>\n',
            "",
        )
        page = MODULE.extract_current_template_dataset_id(page_html, "15056854")
        self.assertIsNotNone(page)
        self.assertIsNone(page["public_data_detail_pk"])
        raw_echoed = sample["resolver"]["response_body"].encode("utf-8")
        with self.assertRaises(ValueError) as caught:
            MODULE.validate_link_resolver_response(
                raw_echoed, "15056854", sample["resolver"]["observed_at"],
                expected_public_data_detail_pk=page["public_data_detail_pk"],
            )
        self.assertEqual(str(caught.exception), "link_resolver_detail_echo_mismatch")
        body_without_echo = (
            '{"status":true,"linkUrl":"http://data.seoul.go.kr/dataList/datasetView.do?infId=OA-109"}'
        ).encode("utf-8")
        accepted = MODULE.validate_link_resolver_response(
            body_without_echo, "15056854", sample["resolver"]["observed_at"],
            expected_public_data_detail_pk=page["public_data_detail_pk"],
        )
        self.assertIsNone(accepted["public_data_detail_pk"])

    def test_malformed_false_nonboolean_duplicate_and_wrong_dataset_echo_fail_closed(self) -> None:
        sample = self.samples["15056854"]
        base = json.loads(sample["resolver"]["response_body"])
        bad_cases = (
            (b"{not-json", "link_resolver_json_invalid"),
            (json.dumps({**base, "status": False}).encode(), "link_resolver_status_or_shape_invalid"),
            (json.dumps({**base, "status": "true"}).encode(), "link_resolver_status_or_shape_invalid"),
            (b'{"status":true,"status":false,"linkUrl":"http://data.seoul.gov/"}', "link_resolver_json_duplicate_key"),
            (json.dumps({**base, "publicDataPk": "15056855"}).encode(), "link_resolver_dataset_echo_mismatch"),
        )
        for body, code in bad_cases:
            with self.subTest(code=code, body=body[:80]), self.assertRaises(ValueError) as caught:
                self.validate(sample, body)
            self.assertEqual(str(caught.exception), code)

    def test_secret_urls_and_unsafe_hosts_or_schemes_fail_closed(self) -> None:
        sample = self.samples["15056854"]
        payload = json.loads(sample["resolver"]["response_body"])
        unsafe_cases = (
            ("http://data.seoul.go.kr/dataList?serviceKey=secret", "link_resolver_url_unsafe"),
            ("http://localhost/admin", "link_resolver_url_unsafe"),
            ("javascript:alert(1)", "link_resolver_url_unsafe"),
            ("https://user:secret@api.example.gov/data", "link_resolver_url_unsafe"),
        )
        for url, code in unsafe_cases:
            with self.subTest(url=url):
                body = json.dumps({**payload, "linkUrl": url}).encode("utf-8")
                self.assert_rejected(sample, body, code)

    def test_secret_query_names_are_normalized_before_resolver_url_retention(self) -> None:
        sample = self.samples["15056854"]
        payload = json.loads(sample["resolver"]["response_body"])
        secret_urls = (
            "http://data.seoul.go.kr/dataList?password=SYNTHETIC_TEST_VALUE",
            "http://data.seoul.go.kr/dataList?client_secret=SYNTHETIC_TEST_VALUE",
            "http://data.seoul.go.kr/dataList?client-secret=SYNTHETIC_TEST_VALUE",
            "http://data.seoul.go.kr/dataList?client%5Fsecret=SYNTHETIC_TEST_VALUE",
            "http://data.seoul.go.kr/dataList?CLIENT%2DSECRET=SYNTHETIC_TEST_VALUE",
            "http://data.seoul.go.kr/dataList?refresh_token=SYNTHETIC_TEST_VALUE",
            "http://data.seoul.go.kr/dataList?refresh-token=SYNTHETIC_TEST_VALUE",
            "http://data.seoul.go.kr/dataList?refresh%5Ftoken=SYNTHETIC_TEST_VALUE",
        )
        for url in secret_urls:
            with self.subTest(url=url):
                body = json.dumps({**payload, "linkUrl": url}).encode("utf-8")
                self.assert_rejected(sample, body, "link_resolver_url_unsafe")

    def test_persisted_link_metadata_rejects_secret_query_keys_after_digest_rebinding(self) -> None:
        dataset_id = "15056854"
        safe_url = "http://data.seoul.go.kr/dataList/datasetView.do?infId=OA-109"
        page_time = "2026-10-05T00:00:00Z"
        resolver_time = "2026-10-05T00:00:01Z"

        def metadata_for(resolved_url: str) -> dict:
            return {
                "method": MODULE.DETAIL_HELPERS.CURRENT_LINK_RESOLVER_METHOD,
                "dataset_id": dataset_id,
                "public_data_pk": dataset_id,
                "public_data_detail_pk": "uddi:fixture-current-template",
                "page": {
                    "url": MODULE.DETAIL_HELPERS.CURRENT_DETAIL_PAGE_URL.format(dataset_id=dataset_id),
                    "effective_url": MODULE.DETAIL_HELPERS.CURRENT_DETAIL_PAGE_URL.format(dataset_id=dataset_id),
                    "sha256": "a" * 64,
                    "bytes": 100,
                    "observed_at": page_time,
                },
                "resolver": {
                    "request_url": MODULE.DETAIL_HELPERS.CURRENT_LINK_RESOLVER_URL.format(dataset_id=dataset_id),
                    "effective_url": MODULE.DETAIL_HELPERS.CURRENT_LINK_RESOLVER_URL.format(dataset_id=dataset_id),
                    "sha256": "b" * 64,
                    "bytes": 100,
                    "observed_at": resolver_time,
                    "public_data_detail_pk": "uddi:fixture-current-template",
                    "resolved_url": resolved_url,
                    "resolved_url_sha256": hashlib.sha256(resolved_url.encode("utf-8")).hexdigest(),
                },
            }

        accepted = MODULE.DETAIL_HELPERS.validate_link_metadata(metadata_for(safe_url), dataset_id, {"data.seoul.go.kr"})
        self.assertEqual(accepted["resolver"]["resolved_url"], safe_url)
        self.assertEqual(accepted["resolver"]["resolved_url_sha256"], hashlib.sha256(safe_url.encode()).hexdigest())

        secret_urls = (
            "http://data.seoul.go.kr/dataList?password=SYNTHETIC_TEST_VALUE",
            "http://data.seoul.go.kr/dataList?client_secret=SYNTHETIC_TEST_VALUE",
            "http://data.seoul.go.kr/dataList?client%5Fsecret=SYNTHETIC_TEST_VALUE",
            "http://data.seoul.go.kr/dataList?refresh_token=SYNTHETIC_TEST_VALUE",
        )
        for url in secret_urls:
            with self.subTest(url=url), self.assertRaises(ValueError) as caught:
                MODULE.DETAIL_HELPERS.validate_link_metadata(metadata_for(url), dataset_id, {"data.seoul.go.kr"})
            self.assertEqual(str(caught.exception), "link_resolver_url_unsafe")

    def test_resolver_response_size_and_time_validation_are_bounded(self) -> None:
        sample = self.samples["15056854"]
        self.assert_rejected(sample, b"{" + b" " * (64 * 1024), "link_resolver_response_size_invalid")
        with self.assertRaises(ValueError) as caught:
            MODULE.validate_link_resolver_response(
                sample["resolver"]["response_body"].encode(), sample["dataset_id"], "not-a-time",
                expected_public_data_detail_pk="uddi:1ca4ede2-911d-486a-b374-e7cf8650d25c_201712281924",
            )
        self.assertEqual(str(caught.exception), "link_resolver_observation_time_invalid")


class LinkResolverTransportTests(unittest.TestCase):
    class Response:
        def __init__(self, body: bytes, *, code: int = 200, url: str, headers: dict[str, str] | None = None) -> None:
            self.body = body
            self.code = code
            self.url = url
            self.headers = headers or {"Content-Type": "application/json;charset=UTF-8", "Content-Encoding": "identity"}
            self.read_sizes: list[int] = []

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def getcode(self) -> int:
            return self.code

        def geturl(self) -> str:
            return self.url

        def read(self, size: int = -1) -> bytes:
            self.read_sizes.append(size)
            return self.body[:size]

    class Opener:
        def __init__(self, response) -> None:
            self.response = response
            self.calls: list[tuple[object, float]] = []

        def open(self, request, timeout: float):
            self.calls.append((request, timeout))
            return self.response

    def setUp(self) -> None:
        fixture = json.loads(FIXTURE.read_text(encoding="utf-8"))
        self.sample = fixture["samples"][0]
        self.url = self.sample["resolver"]["requested_url"]
        self.body = self.sample["resolver"]["response_body"].encode("utf-8")

    def test_resolver_transport_uses_one_bounded_get_on_the_fixed_portal_route(self) -> None:
        response = self.Response(self.body, url=self.url)
        opener = self.Opener(response)
        built_handlers: list[tuple[object, ...]] = []

        def build_opener(*handlers):
            built_handlers.append(handlers)
            return opener

        with mock.patch.object(MODULE.urllib.request, "build_opener", side_effect=build_opener):
            observed = MODULE.fetch_link_resolver(self.url, timeout=12)
        self.assertEqual(len(opener.calls), 1)
        request, timeout = opener.calls[0]
        self.assertEqual(timeout, 12)
        self.assertLessEqual(timeout, 30)
        self.assertEqual(request.get_method(), "GET")
        self.assertEqual(request.full_url, self.url)
        self.assertEqual(request.headers.get("Accept"), "application/json")
        self.assertEqual(request.headers.get("Accept-encoding"), "identity")
        self.assertNotIn("Cookie", request.headers)
        self.assertNotIn("Authorization", request.headers)
        self.assertEqual(response.read_sizes, [64 * 1024 + 1])
        self.assertEqual(observed.body, self.body)
        self.assertEqual(observed.request_url, self.url)
        self.assertEqual(observed.effective_url, self.url)
        self.assertEqual(len(built_handlers), 1)
        self.assertTrue(any(isinstance(handler, MODULE.RejectRedirectHandler) for handler in built_handlers[0]))

    def test_redirect_is_rejected_without_following_the_returned_target(self) -> None:
        target = "https://api.external.example/operation"
        request = MODULE.urllib.request.Request(self.url, method="GET")
        handler = MODULE.RejectRedirectHandler()
        with self.assertRaises(MODULE.UnsafeDetailRedirectError):
            handler.redirect_request(request, None, 302, "Found", {"Location": target}, target)

        redirect_response = self.Response(b"", code=302, url=target)
        opener = self.Opener(redirect_response)
        with mock.patch.object(MODULE.urllib.request, "build_opener", return_value=opener):
            with self.assertRaises(MODULE.UnsafeDetailRedirectError):
                MODULE.fetch_link_resolver(self.url, timeout=12)
        self.assertEqual(len(opener.calls), 1)
        self.assertEqual(redirect_response.read_sizes, [])

    def test_oversized_response_is_rejected_at_the_declared_or_streamed_limit(self) -> None:
        large_content_length = self.Response(
            b"", url=self.url, headers={"Content-Type": "application/json", "Content-Length": str(64 * 1024 + 1)},
        )
        opener = self.Opener(large_content_length)
        with mock.patch.object(MODULE.urllib.request, "build_opener", return_value=opener):
            with self.assertRaises(MODULE.DetailResponseBytesCapError):
                MODULE.fetch_link_resolver(self.url, timeout=12)
        self.assertEqual(len(opener.calls), 1)
        self.assertEqual(large_content_length.read_sizes, [])

        streamed = self.Response(b"x" * (64 * 1024 + 2), url=self.url)
        opener = self.Opener(streamed)
        with mock.patch.object(MODULE.urllib.request, "build_opener", return_value=opener):
            with self.assertRaises(MODULE.DetailResponseBytesCapError):
                MODULE.fetch_link_resolver(self.url, timeout=12)
        self.assertEqual(len(opener.calls), 1)
        self.assertEqual(streamed.read_sizes, [64 * 1024 + 1])

    def test_unsafe_resolver_urls_are_rejected_before_opening(self) -> None:
        unsafe_urls = (
            "https://api.external.example/tcs/dss/selectApiLinkUrl.do?publicDataPk=15056854",
            "https://www.data.go.kr/tcs/dss/selectApiLinkUrl.do?publicDataPk=15056854&extra=1",
            "https://www.data.go.kr/tcs/dss/selectApiLinkUrl.do?publicDataPk=15056854#fragment",
            "http://www.data.go.kr/tcs/dss/selectApiLinkUrl.do?publicDataPk=15056854",
        )
        with mock.patch.object(MODULE.urllib.request, "build_opener") as build_opener:
            for url in unsafe_urls:
                with self.subTest(url=url), self.assertRaisesRegex(ValueError, "unsafe_link_resolver_request_url"):
                    MODULE.fetch_link_resolver(url, timeout=12)
        build_opener.assert_not_called()


class PublicDetailUrllibTransportTests(unittest.TestCase):
    URL = "https://www.data.go.kr/data/15056854/openapi.do"

    class RecordingBytesIO(io.BytesIO):
        def __init__(self, body: bytes) -> None:
            super().__init__(body)
            self.read_sizes: list[int] = []

        def read(self, size: int = -1) -> bytes:
            self.read_sizes.append(size)
            return super().read(size)

    class FakeHTTPSHandler(urllib.request.HTTPSHandler):
        handler_order = 200

        def __init__(self, responses: list[tuple[int, bytes, dict[str, str]]]) -> None:
            super().__init__()
            self.responses = responses
            self.calls: list[urllib.request.Request] = []
            self.readers: list[PublicDetailUrllibTransportTests.RecordingBytesIO] = []

        def https_open(self, request: urllib.request.Request):
            self.calls.append(request)
            index = min(len(self.calls) - 1, len(self.responses) - 1)
            code, body, header_values = self.responses[index]
            headers = Message()
            for name, value in header_values.items():
                headers[name] = value
            reader = PublicDetailUrllibTransportTests.RecordingBytesIO(body)
            self.readers.append(reader)
            response = urllib.response.addinfourl(reader, headers, request.full_url, code)
            response.msg = "OK" if code == 200 else "Found"
            return response

    def build_real_opener_with_fake_https(self, transport):
        real_build_opener = MODULE.urllib.request.build_opener
        captures: list[tuple[tuple[object, ...], object]] = []

        def build_opener(*configured_handlers):
            opener = real_build_opener(*configured_handlers, transport)
            captures.append((configured_handlers, opener))
            return opener

        patcher = mock.patch.object(MODULE.urllib.request, "build_opener", side_effect=build_opener)
        return patcher, captures

    def test_same_url_redirect_is_rejected_after_one_physical_get(self) -> None:
        transport = self.FakeHTTPSHandler([
            (302, b"", {"Location": self.URL}),
        ])
        patcher, captures = self.build_real_opener_with_fake_https(transport)
        with patcher:
            with self.assertRaises(MODULE.UnsafeDetailRedirectError):
                MODULE.fetch_public_detail(self.URL, timeout=12)
        self.assertEqual(len(transport.calls), 1)
        self.assertEqual(transport.calls[0].get_method(), "GET")
        self.assertEqual(transport.calls[0].full_url, self.URL)
        self.assertEqual(transport.readers[0].read_sizes, [])
        opener = captures[0][1]
        self.assertTrue(any(
            isinstance(handler, MODULE.urllib.request.HTTPErrorProcessor)
            for handler in opener.handlers
        ))
        self.assertTrue(any(
            isinstance(handler, MODULE.urllib.request.HTTPRedirectHandler)
            for handler in opener.handlers
        ))

    def test_successful_page_response_retains_bounded_bytes_and_hash(self) -> None:
        body = b"<html><main>bounded public detail response</main></html>"
        transport = self.FakeHTTPSHandler([
            (200, body, {"Content-Type": "text/html; charset=UTF-8", "Content-Length": str(len(body))}),
        ])
        patcher, _captures = self.build_real_opener_with_fake_https(transport)
        with patcher:
            observation = MODULE.fetch_public_detail(self.URL, timeout=12)
        self.assertEqual(len(transport.calls), 1)
        self.assertEqual(transport.calls[0].get_method(), "GET")
        self.assertEqual(transport.calls[0].full_url, self.URL)
        self.assertEqual(transport.readers[0].read_sizes, [MODULE.MAX_DETAIL_BYTES + 1])
        self.assertLessEqual(len(observation.page_bytes), MODULE.MAX_DETAIL_BYTES)
        self.assertEqual(observation.page_bytes, body)
        self.assertEqual(observation.body, body.decode("utf-8"))
        self.assertEqual(observation.page_url, self.URL)
        self.assertEqual(observation.effective_url, self.URL)
        self.assertEqual(observation.page_sha256, hashlib.sha256(body).hexdigest())

    def test_ambient_https_proxy_credentials_are_ignored_by_the_actual_opener(self) -> None:
        body = b"<html>proxy-free response</html>"
        transport = self.FakeHTTPSHandler([
            (200, body, {"Content-Type": "text/html", "Content-Length": str(len(body))}),
        ])
        patcher, captures = self.build_real_opener_with_fake_https(transport)
        proxy_environment = {
            "HTTPS_PROXY": "https://user:secret@proxy.invalid:8443",
            "https_proxy": "https://user:secret@proxy.invalid:8443",
            "HTTP_PROXY": "http://user:secret@proxy.invalid:8080",
            "http_proxy": "http://user:secret@proxy.invalid:8080",
        }
        with mock.patch.dict(os.environ, proxy_environment, clear=False), patcher:
            observation = MODULE.fetch_public_detail(self.URL, timeout=12)

        self.assertEqual(observation.page_bytes, body)
        self.assertEqual(len(transport.calls), 1)
        request = transport.calls[0]
        self.assertEqual(request.host, "www.data.go.kr")
        self.assertEqual(request.full_url, self.URL)
        request_header_names = {name.lower() for name, _value in request.header_items()}
        self.assertNotIn("proxy-authorization", request_header_names)
        self.assertNotIn("authorization", request_header_names)
        configured, opener = captures[0]
        empty_proxy_handlers = [
            handler for handler in configured
            if isinstance(handler, MODULE.urllib.request.ProxyHandler) and handler.proxies == {}
        ]
        self.assertEqual(len(empty_proxy_handlers), 1)
        effective_proxy_handlers = [
            handler for handler in opener.handlers
            if isinstance(handler, MODULE.urllib.request.ProxyHandler)
        ]
        # urllib omits an empty ProxyHandler from opener.handlers because it
        # has no proxy_open methods to register; the explicit empty handler
        # supplied to the real builder above prevents its environment default.
        self.assertEqual(effective_proxy_handlers, [])


class LinkResolverWorkerIntegrationTests(unittest.TestCase):
    NOW = "2026-10-05T00:00:02Z"

    @classmethod
    def setUpClass(cls) -> None:
        fixture = json.loads(FIXTURE.read_text(encoding="utf-8"))
        cls.samples = {row["dataset_id"]: row for row in fixture["samples"]}
        registry = json.loads((ROOT / "data" / "data-go-kr.registry.json").read_text(encoding="utf-8"))
        sample_ids = set(cls.samples)
        cls.historical_rows = {
            row["id"]: row for row in registry if isinstance(row, dict) and row.get("id") in sample_ids
        }
        if set(cls.historical_rows) != sample_ids:
            raise AssertionError("native fixture rows are missing from the checked-in registry")

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="link-detail-resolver-worker-")
        self.tmp = pathlib.Path(self.temp.name)
        self.baseline_path = self.tmp / "baseline.registry.json"
        self.candidate_path = self.tmp / "candidate.registry.json"
        self.diff_path = self.tmp / "catalogue-diff.json"
        self.refresh_evidence_path = self.tmp / "refresh-evidence.json"
        self.state_dir = self.tmp / "state"
        self.output_dir = self.tmp / "output"

    def tearDown(self) -> None:
        self.temp.cleanup()

    @staticmethod
    def _write_json(path: pathlib.Path, value: object) -> bytes:
        data = (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode("utf-8")
        path.write_bytes(data)
        return data

    def _inputs(
        self, identities: tuple[str, ...], *, empty_baseline_ids: tuple[str, ...] = (),
    ) -> tuple[list[dict], list[dict], object]:
        baseline = [copy.deepcopy(self.historical_rows[identity]) for identity in identities]
        for row in baseline:
            if row["id"] in empty_baseline_ids:
                row["operations"] = []
        candidate = copy.deepcopy(baseline)
        for row in candidate:
            row["operations"] = []
            row["source"]["raw"]["guide_url"] = f"https://www.data.go.kr/guide/manual/{row['id']}.html"
        baseline_bytes = self._write_json(self.baseline_path, baseline)
        candidate_bytes = self._write_json(self.candidate_path, candidate)
        stable_ids = set(identities)
        changes = []
        for before, after in zip(baseline, candidate):
            changes.append({
                "id": before["id"],
                "fields": ["operations", "source"],
                "old_digest": MODULE.sha256_bytes(MODULE.canonical_json(before)),
                "new_digest": MODULE.sha256_bytes(MODULE.canonical_json(after)),
            })
        summary = {"added": 0, "removed": 0, "changed": len(changes), "stable": 0}
        diff = {
            "generated_at": self.NOW,
            "provider": "data.go.kr",
            "old": self.baseline_path.name,
            "new": self.candidate_path.name,
            "limit": 0,
            "truncated": False,
            "counts": {"old": len(baseline), "new": len(candidate)},
            "summary": summary,
            "added": [],
            "removed": [],
            "changed": changes,
        }
        diff_bytes = self._write_json(self.diff_path, diff)
        evidence = {
            "schema_version": "datapan.upstream-refresh-evidence.v1",
            "observed_at": self.NOW,
            "source_id": "data_go_kr",
            "owner": "data_go_kr",
            "status": "material_change",
            "collection": {"attempted": True, "succeeded": True, "exit_code": 0, "error_class": None},
            "baseline": {
                "path": self.baseline_path.name,
                "bytes": len(baseline_bytes),
                "sha256": hashlib.sha256(baseline_bytes).hexdigest(),
                "records": len(baseline),
            },
            "snapshot": {
                "path": self.candidate_path.name,
                "bytes": len(candidate_bytes),
                "sha256": hashlib.sha256(candidate_bytes).hexdigest(),
                "records": len(candidate),
            },
            "diff": {
                "path": self.diff_path.name,
                "sha256": hashlib.sha256(diff_bytes).hexdigest(),
                "summary": summary,
            },
            "review": {"action": "review_catalog_drift", "work_key": "upstream-refresh:data_go_kr:0123456789abcdef"},
            "publication": {"automatic": False, "release_allowed": False, "required_gates": ["review", "validation", "publication"]},
        }
        self._write_json(self.refresh_evidence_path, evidence)
        args = MODULE.build_parser().parse_args([
            "--baseline", str(self.baseline_path),
            "--candidate", str(self.candidate_path),
            "--diff", str(self.diff_path),
            "--refresh-evidence", str(self.refresh_evidence_path),
            "--source-policy", str(ROOT / "policy" / "source-refresh.json"),
            "--provider-index", str(ROOT / "data" / "provider-index.json"),
            "--state-dir", str(self.state_dir),
            "--output-dir", str(self.output_dir),
            "--checkpoint-schema", str(ROOT / "schemas" / "datapan.upstream-catalogue-checkpoint.v1.schema.json"),
            "--composer", str(ROOT / "scripts" / "compose-upstream-catalogue-candidate.py"),
            "--producer-run-id", "123456",
            "--processor-run-id", "123456",
            "--processor-artifact-run-id", "123456",
            "--repository", "StatPan/datapan-registry",
            "--execution-mode", "fixture",
            "--producer-run-url", "https://github.com/StatPan/datapan-registry/actions/runs/123456",
            "--now", self.NOW,
            "--max-attempts", "24",
            "--max-queue", "48",
            "--retries-per-detail", "2",
        ])
        self.assertEqual(stable_ids, {row["id"] for row in candidate})
        return baseline, candidate, args

    def _observations(self, *, page_transform=None, resolver_transform=None):
        page_calls: list[tuple[str, float]] = []
        resolver_calls: list[tuple[str, float]] = []

        def page_get(url: str, timeout: float):
            page_calls.append((url, timeout))
            dataset_id = url.rsplit("/", 2)[-2]
            sample = self.samples[dataset_id]
            html = sample["detail_page"]["sanitized_html_excerpt"]
            if page_transform is not None:
                html = page_transform(dataset_id, html)
            raw = html.encode("utf-8")
            resolver_time = datetime.fromisoformat(sample["resolver"]["observed_at"].replace("Z", "+00:00"))
            # This in-process page observation uses a synthetic test time close
            # to the resolver capture; the fixture separately preserves the
            # original manual page timestamp and is never claimed as runtime proof.
            page_observed_at = (resolver_time - timedelta(seconds=1)).isoformat().replace("+00:00", "Z")
            return MODULE.DetailPageObservation(
                body=html,
                page_url=url,
                effective_url=url,
                page_sha256=MODULE.sha256_bytes(raw),
                observed_at=page_observed_at,
                page_bytes=raw,
            )

        def resolver_get(url: str, timeout: float):
            resolver_calls.append((url, timeout))
            dataset_id = url.partition("publicDataPk=")[2]
            sample = self.samples[dataset_id]["resolver"]
            response_body = sample["response_body"]
            if resolver_transform is not None:
                response_body = resolver_transform(dataset_id, response_body)
            raw = response_body.encode("utf-8")
            return MODULE.LinkResolverObservation(
                body=raw,
                request_url=url,
                effective_url=url,
                observed_at=sample["observed_at"],
            )

        return page_get, resolver_get, page_calls, resolver_calls

    def test_real_worker_resolves_manual_metadata_without_replacing_historical_or_empty_operations(self) -> None:
        baseline, _candidate, args = self._inputs(
            ("15056854", "15056858"), empty_baseline_ids=("15056858",),
        )
        page_get, resolver_get, page_calls, resolver_calls = self._observations()
        code, checkpoint = MODULE.process(
            args,
            fetcher=page_get,
            resolver_fetcher=resolver_get,
            sleeper=lambda _delay: None,
            clock=lambda: datetime(2026, 10, 5, 0, 0, 2, tzinfo=timezone.utc),
        )
        self.assertEqual(code, 2, checkpoint.get("outcome"))
        self.assertEqual(checkpoint["attempts_consumed"], 4)
        self.assertEqual(checkpoint["attempts_by_id"], {"15056854": 2, "15056858": 2})
        self.assertEqual(len(page_calls), 2)
        self.assertEqual(len(resolver_calls), 2)
        self.assertLessEqual(len(page_calls) + len(resolver_calls), 24)
        self.assertLessEqual(max(checkpoint["attempts_by_id"].values()), 3)
        self.assertEqual({url for url, _ in page_calls}, {
            "https://www.data.go.kr/data/15056854/openapi.do",
            "https://www.data.go.kr/data/15056858/openapi.do",
        })
        self.assertEqual({url for url, _ in resolver_calls}, {
            "https://www.data.go.kr/tcs/dss/selectApiLinkUrl.do?publicDataPk=15056854",
            "https://www.data.go.kr/tcs/dss/selectApiLinkUrl.do?publicDataPk=15056858",
        })
        self.assertTrue(all(timeout <= 30 for _, timeout in page_calls + resolver_calls))

        enrichment = json.loads((self.output_dir / "upstream-catalogue-enrichment-evidence.json").read_text(encoding="utf-8"))
        self.assertEqual(enrichment["records"], [])
        self.assertEqual([row["api_key"]["id"] for row in enrichment["worker_outcomes"]], ["15056854", "15056858"])
        self.assertEqual({row["failure_diagnostic"]["code"] for row in enrichment["worker_outcomes"]}, {
            "resolved_link_operation_contract_unproven",
        })
        for outcome in enrichment["worker_outcomes"]:
            self.assertEqual(outcome["status"], "quarantined")
            self.assertEqual(outcome["failure_diagnostic"], {
                "code": "resolved_link_operation_contract_unproven", "phase": "resolver",
            })
            self.assertIn("link_metadata", outcome)
            metadata = outcome["link_metadata"]
            sample = self.samples[outcome["api_key"]["id"]]
            self.assertEqual(metadata["dataset_id"], outcome["api_key"]["id"])
            self.assertEqual(metadata["resolver"]["resolved_url"], sample["resolver"]["response_body"].split('"linkUrl":"', 1)[1].split('"', 1)[0])
            self.assertEqual(metadata["resolver"]["sha256"], sample["resolver"]["response_sha256"])
            self.assertEqual(metadata["resolver"]["bytes"], sample["resolver"]["response_size_bytes"])
            self.assertEqual(metadata["resolver"]["public_data_detail_pk"], metadata["public_data_detail_pk"])
            self.assertEqual(set(metadata), {"method", "dataset_id", "public_data_pk", "public_data_detail_pk", "page", "resolver"})
            self.assertNotIn("response_body", metadata["resolver"])

        composed = json.loads((self.output_dir / "composed-candidate.registry.json").read_text(encoding="utf-8"))
        composed_by_id = {row["id"]: row for row in composed}
        baseline_by_id = {row["id"]: row for row in baseline}
        self.assertEqual(composed_by_id["15056854"]["operations"], baseline_by_id["15056854"]["operations"])
        self.assertEqual(len(composed_by_id["15056854"]["operations"]), 3)
        self.assertEqual(composed_by_id["15056858"]["operations"], [])
        self.assertEqual(len(resolver_calls), sum(1 for _ in self.samples.values()))

    def test_wrong_current_button_cannot_fall_back_to_legacy_anchor_or_call_resolver(self) -> None:
        baseline, _candidate, args = self._inputs(("15056854",))

        def wrong_button_with_eligible_anchor(dataset_id: str, html: str) -> str:
            html = html.replace(f"fn_goUrlLink('{dataset_id}')", "fn_goUrlLink('15056855')")
            return html + (
                '<a href="http://data.seoul.go.kr/dataList/datasetView.do?infId=OA-109" '
                'onclick="fn_LinkApiRequest()">legacy-looking API</a>'
            )

        page_get, resolver_get, page_calls, resolver_calls = self._observations(page_transform=wrong_button_with_eligible_anchor)
        code, checkpoint = MODULE.process(
            args,
            fetcher=page_get,
            resolver_fetcher=resolver_get,
            sleeper=lambda _delay: None,
            clock=lambda: datetime(2026, 10, 5, 0, 0, 2, tzinfo=timezone.utc),
        )
        self.assertEqual(code, 2, checkpoint.get("outcome"))
        self.assertEqual(checkpoint["attempts_consumed"], 1)
        self.assertEqual(checkpoint["attempts_by_id"], {"15056854": 1})
        self.assertEqual(len(page_calls), 1)
        self.assertEqual(resolver_calls, [])
        enrichment = json.loads((self.output_dir / "upstream-catalogue-enrichment-evidence.json").read_text(encoding="utf-8"))
        self.assertEqual(enrichment["records"], [])
        self.assertEqual(len(enrichment["worker_outcomes"]), 1)
        outcome = enrichment["worker_outcomes"][0]
        self.assertEqual(outcome["status"], "quarantined")
        self.assertEqual(outcome["failure_diagnostic"], {"code": "contract_or_parse_error"})
        self.assertNotIn("link_metadata", outcome)
        composed = json.loads((self.output_dir / "composed-candidate.registry.json").read_text(encoding="utf-8"))
        self.assertEqual(composed[0]["operations"], baseline[0]["operations"])
        self.assertEqual(len(composed[0]["operations"]), 3)

    def test_unregistered_metadata_host_is_quarantined_without_target_fetch(self) -> None:
        baseline, _candidate, args = self._inputs(
            ("15056858",), empty_baseline_ids=("15056858",),
        )

        def unregistered_url(_dataset_id: str, response_body: str) -> str:
            payload = json.loads(response_body)
            payload["linkUrl"] = "https://unregistered.example.test/guide"
            return json.dumps(payload, separators=(",", ":"))

        page_get, resolver_get, page_calls, resolver_calls = self._observations(resolver_transform=unregistered_url)
        code, checkpoint = MODULE.process(
            args,
            fetcher=page_get,
            resolver_fetcher=resolver_get,
            sleeper=lambda _delay: None,
            clock=lambda: datetime(2026, 10, 5, 0, 0, 2, tzinfo=timezone.utc),
        )
        self.assertEqual(code, 2, checkpoint.get("outcome"))
        self.assertEqual(checkpoint["attempts_consumed"], 2)
        self.assertEqual(len(page_calls), 1)
        self.assertEqual(len(resolver_calls), 1)
        self.assertEqual(resolver_calls[0][0], "https://www.data.go.kr/tcs/dss/selectApiLinkUrl.do?publicDataPk=15056858")
        enrichment = json.loads((self.output_dir / "upstream-catalogue-enrichment-evidence.json").read_text(encoding="utf-8"))
        self.assertEqual(enrichment["records"], [])
        outcome = enrichment["worker_outcomes"][0]
        self.assertEqual(outcome["status"], "quarantined")
        self.assertEqual(outcome["failure_diagnostic"]["code"], "unsafe_or_unregistered_operation_host")
        self.assertNotIn("link_metadata", outcome)
        composed = json.loads((self.output_dir / "composed-candidate.registry.json").read_text(encoding="utf-8"))
        self.assertEqual(composed[0]["operations"], baseline[0]["operations"])
        self.assertEqual(composed[0]["operations"], [])

    def test_secret_bearing_resolver_url_is_quarantined_without_persisting_url_or_value(self) -> None:
        _baseline, _candidate, args = self._inputs(("15056854",))
        secret_value = "SYNTHETIC_TEST_VALUE"

        def secret_url(_dataset_id: str, response_body: str) -> str:
            payload = json.loads(response_body)
            payload["linkUrl"] = "http://data.seoul.go.kr/dataList?client%5Fsecret=" + secret_value
            return json.dumps(payload, separators=(",", ":"))

        page_get, resolver_get, page_calls, resolver_calls = self._observations(resolver_transform=secret_url)
        code, checkpoint = MODULE.process(
            args,
            fetcher=page_get,
            resolver_fetcher=resolver_get,
            sleeper=lambda _delay: None,
            clock=lambda: datetime(2026, 10, 5, 0, 0, 2, tzinfo=timezone.utc),
        )
        self.assertEqual(code, 2, checkpoint.get("outcome"))
        self.assertEqual(len(page_calls), 1)
        self.assertEqual(len(resolver_calls), 1)
        evidence_path = self.output_dir / "upstream-catalogue-enrichment-evidence.json"
        evidence_bytes = evidence_path.read_bytes()
        evidence = json.loads(evidence_bytes)
        outcome = evidence["worker_outcomes"][0]
        self.assertEqual(outcome["status"], "quarantined")
        self.assertEqual(outcome["failure_diagnostic"]["code"], "unsafe_or_unregistered_operation_host")
        self.assertNotIn("link_metadata", outcome)
        self.assertNotIn(secret_value.encode(), evidence_bytes)
        self.assertNotIn(secret_value, json.dumps(checkpoint))


if __name__ == "__main__":
    unittest.main()
