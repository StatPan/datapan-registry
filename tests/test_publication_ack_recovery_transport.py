from __future__ import annotations

import datetime as dt
import importlib.util
import io
import json
import pathlib
import sys
import unittest
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from unittest import mock


ROOT = pathlib.Path(__file__).parents[1]
SCRIPT = ROOT / "scripts" / "recover-canonical-publication-ack.py"
SPEC = importlib.util.spec_from_file_location("recover_canonical_publication_ack_transport_test", SCRIPT)
assert SPEC and SPEC.loader
RECOVERY = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = RECOVERY
SPEC.loader.exec_module(RECOVERY)

FIXTURE_DIR = ROOT / "tests" / "fixtures" / "canonical-publication-ack" / "incident-37199628001-1"
REST_CONTRACT_2026_DIR = ROOT / "tests" / "fixtures" / "canonical-publication-ack" / "rest-version-contract-2026-03-10"
REST_CONTRACT_2022_DIR = ROOT / "tests" / "fixtures" / "canonical-publication-ack" / "rest-version-contract-2022-11-28"
REPOSITORY = "StatPan/datapan-registry"
PEER_SOURCE_SHA = "849af2936a743573358820275df985b5809d0f7b"


def load_contract_fixture(path: pathlib.Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


class FixtureRunner:
    """Load the real journal identity helper used by publication classification."""

    @staticmethod
    def load_module(path: pathlib.Path, name: str):
        spec = importlib.util.spec_from_file_location(name, path)
        assert spec and spec.loader
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)
        return module


class MemoryResponse:
    def __init__(self, status: int, headers: dict[str, str], body: bytes) -> None:
        self.status = status
        self.headers = headers
        self.body = body

    def read(self, maximum: int = -1) -> bytes:
        return self.body if maximum < 0 else self.body[:maximum]

    def __enter__(self) -> "MemoryResponse":
        return self

    def __exit__(self, *_: object) -> None:
        return None


class RedirectResponse:
    def __init__(self, target: str) -> None:
        self.target = target


class RedirectingOpener:
    """Exercise urllib's real redirect-handler path without opening sockets."""

    def __init__(self, handler: RECOVERY.SafeRedirectHandler, route) -> None:
        self.handler = handler
        self.handler.parent = self
        self.route = route
        self.requests: list[urllib.request.Request] = []

    def open(self, request: urllib.request.Request, timeout: int | None = None) -> MemoryResponse:
        request.timeout = timeout
        self.requests.append(request)
        result = self.route(request)
        if isinstance(result, RedirectResponse):
            headers = {"location": result.target}
            response_file = io.BytesIO(b"")
            redirected = self.handler.http_error_302(request, response_file, 302, "Found", headers)
            if redirected is None:
                raise urllib.error.HTTPError(request.full_url, 302, "redirect rejected", headers, io.BytesIO(b""))
            return redirected
        status, headers, body = result
        if status != 200:
            raise urllib.error.HTTPError(request.full_url, status, "fixture response", headers, io.BytesIO(body))
        return MemoryResponse(status, headers, body)


class PublicationAckRecoveryTransportTests(unittest.TestCase):
    def api(self, transport, *, sleep=None):
        return RECOVERY.GitHubApi(
            "dummy-test-marker",
            transport=transport,
            sleep=sleep or (lambda _seconds: None),
        )

    def test_redirect_and_retry_each_debit_the_same_request_budget(self) -> None:
        api_url = "https://api.github.com/repos/StatPan/datapan-registry/actions/runs/1"
        signed_url = "https://objects.example.test/download?signature=fixture"
        api_attempts = 0
        openers: list[RedirectingOpener] = []

        def route(request: urllib.request.Request):
            nonlocal api_attempts
            if request.full_url == api_url:
                api_attempts += 1
                if api_attempts == 1:
                    return RedirectResponse(signed_url)
                return 200, {}, b"ok"
            if request.full_url == signed_url:
                return 503, {}, b"retry"
            raise AssertionError(f"unexpected fixture URL: {request.full_url}")

        def build_opener(handler):
            opener = RedirectingOpener(handler, route)
            openers.append(opener)
            return opener

        api = RECOVERY.GitHubApi("dummy-test-marker", request_limit=3, sleep=lambda _seconds: None)
        with mock.patch.object(RECOVERY.urllib.request, "build_opener", side_effect=build_opener):
            response = api.request("repos/StatPan/datapan-registry/actions/runs/1")

        self.assertEqual(response.body, b"ok")
        self.assertEqual(api.request_count, 3)  # initial API request + redirect hop + retry
        self.assertEqual(len(openers), 2)
        first_initial, redirected = openers[0].requests
        retry_initial = openers[1].requests[0]
        self.assertEqual(first_initial.get_header("Authorization"), "Bearer dummy-test-marker")
        self.assertEqual(retry_initial.get_header("Authorization"), "Bearer dummy-test-marker")
        self.assertEqual(redirected.full_url, signed_url)
        self.assertEqual(dict(redirected.header_items()), {"Accept": "application/octet-stream"})

    def test_http_redirect_is_rejected_before_following_or_debiting_a_hop(self) -> None:
        api_url = "https://api.github.com/repos/StatPan/datapan-registry/actions/runs/2"
        openers: list[RedirectingOpener] = []

        def route(request: urllib.request.Request):
            if request.full_url == api_url:
                return RedirectResponse("http://attacker.example.test/collect")
            raise AssertionError("an HTTP redirect target must never be opened")

        def build_opener(handler):
            opener = RedirectingOpener(handler, route)
            openers.append(opener)
            return opener

        api = RECOVERY.GitHubApi("dummy-test-marker")
        with mock.patch.object(RECOVERY.urllib.request, "build_opener", side_effect=build_opener):
            with self.assertRaisesRegex(RECOVERY.RecoveryError, "github_api_http_302"):
                api.request("repos/StatPan/datapan-registry/actions/runs/2")

        self.assertEqual(api.request_count, 1)
        self.assertEqual(len(openers[0].requests), 1)

    def test_redirect_limit_allows_four_hops_and_rejects_the_fifth(self) -> None:
        start = "https://api.github.com/repos/StatPan/datapan-registry/actions/runs/3"
        targets = [f"https://objects.example.test/hop/{index}" for index in range(1, 6)]
        openers: list[RedirectingOpener] = []

        def route(request: urllib.request.Request):
            if request.full_url == start:
                return RedirectResponse(targets[0])
            index = targets.index(request.full_url)
            if index < len(targets) - 1:
                return RedirectResponse(targets[index + 1])
            return 200, {}, b"unexpected fifth hop"

        def build_opener(handler):
            opener = RedirectingOpener(handler, route)
            openers.append(opener)
            return opener

        api = RECOVERY.GitHubApi("dummy-test-marker")
        with mock.patch.object(RECOVERY.urllib.request, "build_opener", side_effect=build_opener):
            with self.assertRaisesRegex(RECOVERY.RecoveryError, "github_api_http_302"):
                api.request("repos/StatPan/datapan-registry/actions/runs/3")

        requested_urls = [request.full_url for request in openers[0].requests]
        self.assertEqual(requested_urls, [start, *targets[:4]])
        self.assertEqual(api.request_count, 6)  # initial request plus five redirect responses, last denied

    def test_permanent_four_xx_is_not_retried(self) -> None:
        calls: list[urllib.request.Request] = []

        def transport(request, _timeout, _maximum):
            calls.append(request)
            return RECOVERY.HttpResponse(404, {}, b"missing")

        api = self.api(transport)
        with self.assertRaisesRegex(RECOVERY.RecoveryError, "github_api_http_404"):
            api.request("repos/StatPan/datapan-registry/actions/runs/404")

        self.assertEqual(len(calls), 1)
        self.assertEqual(api.request_count, 1)

    def test_transient_retries_are_bounded_and_clamped(self) -> None:
        calls: list[urllib.request.Request] = []
        delays: list[float] = []

        def transport(request, _timeout, _maximum):
            calls.append(request)
            return RECOVERY.HttpResponse(503, {"Retry-After": "900"}, b"unavailable")

        api = self.api(transport, sleep=delays.append)
        with self.assertRaisesRegex(RECOVERY.RecoveryError, "github_api_http_503"):
            api.request("repos/StatPan/datapan-registry/actions/runs/retry")

        self.assertEqual(len(calls), RECOVERY.MAX_RETRIES + 1)
        self.assertEqual(api.request_count, RECOVERY.MAX_RETRIES + 1)
        self.assertEqual(delays, [5.0, 5.0])

    def test_shared_256_request_limit_stops_before_an_extra_transport_call(self) -> None:
        calls: list[urllib.request.Request] = []

        def transport(request, _timeout, _maximum):
            calls.append(request)
            return RECOVERY.HttpResponse(200, {}, b"{}")

        api = RECOVERY.GitHubApi("dummy-test-marker", transport=transport)
        for index in range(RECOVERY.MAX_GITHUB_REQUESTS):
            api.request(f"repos/StatPan/datapan-registry/actions/runs/{index + 1}")
        with self.assertRaisesRegex(RECOVERY.RecoveryError, "github_request_limit_exceeded"):
            api.request(f"repos/StatPan/datapan-registry/actions/runs/{RECOVERY.MAX_GITHUB_REQUESTS + 1}")

        self.assertEqual(api.request_count, RECOVERY.MAX_GITHUB_REQUESTS)
        self.assertEqual(len(calls), RECOVERY.MAX_GITHUB_REQUESTS)

    def test_oversized_json_response_is_rejected_by_the_api_client(self) -> None:
        api = self.api(lambda *_args: RECOVERY.HttpResponse(200, {}, b"{" + b" " * RECOVERY.MAX_JSON_BYTES))

        with self.assertRaisesRegex(RECOVERY.RecoveryError, "github_api_response_too_large"):
            api.json("repos/StatPan/datapan-registry")

    def test_oversized_zip_and_member_are_rejected_before_json_parsing(self) -> None:
        with self.assertRaisesRegex(RECOVERY.RecoveryError, "publication_archive_size_invalid"):
            RECOVERY.extract_receipt_archive(b"x" * (RECOVERY.MAX_ARCHIVE_BYTES + 1))

        raw = io.BytesIO()
        with zipfile.ZipFile(raw, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            archive.writestr(RECOVERY.RECEIPT_MEMBER, b"0" * (RECOVERY.MAX_JSON_BYTES + 1))
            archive.writestr(RECOVERY.SOURCE_BINDING_MEMBER, b"{}")
        self.assertLess(len(raw.getvalue()), RECOVERY.MAX_ARCHIVE_BYTES)
        with self.assertRaisesRegex(RECOVERY.RecoveryError, "publication_archive_member_unsafe"):
            RECOVERY.extract_receipt_archive(raw.getvalue())

    def test_api_artifact_declared_size_must_match_downloaded_fixture(self) -> None:
        publisher_run = json.loads((FIXTURE_DIR / "publisher-run.json").read_text(encoding="utf-8"))
        publisher_jobs = json.loads((FIXTURE_DIR / "publisher-jobs.json").read_text(encoding="utf-8"))
        publisher_artifacts = json.loads((FIXTURE_DIR / "publisher-artifacts.json").read_text(encoding="utf-8"))
        receipt_artifacts = [
            row for row in publisher_artifacts["artifacts"]
            if row.get("name") == RECOVERY.RECEIPT_ARTIFACT_NAME
        ]
        self.assertEqual(len(receipt_artifacts), 1)
        receipt_artifact = receipt_artifacts[0]
        receipt_artifact["size_in_bytes"] = (FIXTURE_DIR / "publication-receipts.zip").stat().st_size + 1
        archive = (FIXTURE_DIR / "publication-receipts.zip").read_bytes()
        run_id = str(publisher_run["id"])
        attempt = str(publisher_run["run_attempt"])
        routes = {
            f"/repos/{REPOSITORY}/actions/runs/{run_id}/attempts/{attempt}": publisher_run,
            f"/repos/{REPOSITORY}/actions/runs/{run_id}/attempts/{attempt}/jobs?per_page=100": publisher_jobs,
            f"/repos/{REPOSITORY}/actions/runs/{run_id}/artifacts?per_page=100": publisher_artifacts,
            f"/repos/{REPOSITORY}/actions/runs/{run_id}": publisher_run,
        }
        requested: list[str] = []

        def transport(request, _timeout, _maximum):
            path = urllib.parse.urlsplit(request.full_url).path
            requested.append(path)
            if path == f"/repos/{REPOSITORY}/actions/artifacts/{receipt_artifact['id']}/zip":
                return RECOVERY.HttpResponse(200, {"Content-Length": str(len(archive))}, archive)
            payload = routes.get(path + ("?" + urllib.parse.urlsplit(request.full_url).query if urllib.parse.urlsplit(request.full_url).query else ""))
            if payload is None:
                raise AssertionError(f"unexpected fixture API path: {path}")
            return RECOVERY.HttpResponse(200, {}, json.dumps(payload).encode("utf-8"))

        api = self.api(transport)
        recovery = RECOVERY.PublicationAckRecovery(
            ROOT, REPOSITORY, api, runner=None,
            now=lambda: dt.datetime(2026, 10, 4, 12, 0, tzinfo=dt.timezone.utc),
        )
        recovery.repository_id = publisher_artifacts["artifacts"][0]["workflow_run"]["repository_id"]
        recovery.default_branch = "main"
        recovery.publisher_workflow_id = publisher_run["workflow_id"]

        with self.assertRaisesRegex(RECOVERY.RecoveryError, "publication_archive_api_size_mismatch"):
            recovery.authenticate_publisher(publisher_run, expected_attempt=publisher_run["run_attempt"])

        self.assertTrue(any(path.endswith("/zip") for path in requested))
        self.assertEqual(api.request_count, len(requested))

    def test_all_recovery_rest_requests_pin_supported_api_version(self) -> None:
        seen: list[tuple[str, str | None]] = []

        def transport(request, _timeout, _maximum):
            parsed = urllib.parse.urlsplit(request.full_url)
            seen.append((parsed.path + ("?" + parsed.query if parsed.query else ""), request.get_header("X-github-api-version")))
            return RECOVERY.HttpResponse(200, {}, b"{}")

        api = self.api(transport)
        api.request(f"repos/{REPOSITORY}/commits/{PEER_SOURCE_SHA}/pulls?per_page=100")
        api.request(f"repos/{REPOSITORY}/pulls/720")

        self.assertEqual(RECOVERY.GITHUB_REST_API_VERSION, "2022-11-28")
        self.assertEqual([path.rsplit("/", 1)[-1] for path, _ in seen], ["pulls?per_page=100", "720"])
        self.assertTrue(all(version == "2022-11-28" for _, version in seen))
        self.assertEqual(api.request_count, 2)

    def test_versioned_native_association_without_merge_sha_fails_closed_without_fallback(self) -> None:
        fixture = load_contract_fixture(REST_CONTRACT_2026_DIR / "association-2026-03-10.json")
        self.assertEqual(fixture["response_api_version_selected"], "2026-03-10")
        self.assertEqual(fixture["response"][0]["number"], 720)
        self.assertNotIn("merge_commit_sha", fixture["response"][0])
        requests: list[str] = []

        def transport(request, _timeout, _maximum):
            requests.append(urllib.parse.urlsplit(request.full_url).path + "?" + urllib.parse.urlsplit(request.full_url).query)
            return RECOVERY.HttpResponse(200, {}, json.dumps(fixture["response"]).encode("utf-8"))

        recovery = RECOVERY.PublicationAckRecovery(ROOT, REPOSITORY, self.api(transport), FixtureRunner())
        recovery.repository_id = 1278568329
        recovery.default_branch = "main"
        verified = {
            "publisher_job_started_at": "2026-10-04T15:31:14Z",
            "publication": {"source_sha": PEER_SOURCE_SHA, "manifest_sha256": "a" * 64},
        }
        with self.assertRaisesRegex(RECOVERY.RecoveryError, "commit_pull_request_association_ambiguous_or_missing"):
            recovery._commit_pull_request(verified)

        self.assertEqual(requests, [f"/repos/{REPOSITORY}/commits/{PEER_SOURCE_SHA}/pulls?per_page=100"])
        self.assertEqual(recovery.summary["commit_association_reads"], 1)
        self.assertEqual(recovery.summary["pull_request_readbacks"], 0)
        self.assertEqual(recovery.summary["git_cas_transactions"], 0)

    def test_supported_native_association_classifies_exact_peer_outside_c_without_journal_write(self) -> None:
        fixture = load_contract_fixture(REST_CONTRACT_2022_DIR / "association-2022-11-28.json")
        self.assertEqual(fixture["response_api_version_selected"], "2022-11-28")
        association_row = fixture["response"][0]
        self.assertEqual(association_row["merge_commit_sha"], PEER_SOURCE_SHA)
        requests: list[urllib.request.Request] = []

        def transport(request, _timeout, _maximum):
            requests.append(request)
            return RECOVERY.HttpResponse(200, {}, json.dumps(fixture["response"]).encode("utf-8"))

        api = self.api(transport)
        recovery = RECOVERY.PublicationAckRecovery(ROOT, REPOSITORY, api, FixtureRunner())
        recovery.repository_id = 1278568329
        recovery.default_branch = "main"
        verified = {
            "publisher_job_started_at": "2026-10-04T15:31:14Z",
            "publication": {"source_sha": PEER_SOURCE_SHA, "manifest_sha256": "a" * 64},
        }
        disposition, association = recovery.classify_publication(verified, {"records": []})

        self.assertEqual(disposition, "outside_c")
        self.assertIsNotNone(association)
        self.assertEqual(association["number"], 720)
        self.assertEqual(association["merge_sha"], PEER_SOURCE_SHA)
        self.assertEqual(association["head_ref"], "issue-719-preserve-health-endpoint-transport-and-correct-korad-operation-identity")
        self.assertEqual(len(requests), 1)
        self.assertEqual(requests[0].get_header("X-github-api-version"), "2022-11-28")
        self.assertEqual(api.request_count, 1)
        self.assertEqual(recovery.summary["git_cas_transactions"], 0)
        self.assertEqual(recovery.summary["pull_request_readbacks"], 0)

    def test_supported_native_single_pr_readback_retains_exact_merge_sha(self) -> None:
        fixture = load_contract_fixture(REST_CONTRACT_2022_DIR / "pull-2022-11-28.json")
        self.assertEqual(fixture["response_api_version_selected"], "2022-11-28")
        self.assertEqual(fixture["response"]["merge_commit_sha"], PEER_SOURCE_SHA)
        requests: list[urllib.request.Request] = []

        def transport(request, _timeout, _maximum):
            requests.append(request)
            return RECOVERY.HttpResponse(200, {}, json.dumps(fixture["response"]).encode("utf-8"))

        api = self.api(transport)
        recovery = RECOVERY.PublicationAckRecovery(ROOT, REPOSITORY, api, FixtureRunner())
        result = recovery.readback_pr(720)

        self.assertEqual(result["state"], "MERGED")
        self.assertEqual(result["mergeCommit"], {"oid": PEER_SOURCE_SHA})
        self.assertEqual(result["baseRefName"], "main")
        self.assertEqual(result["headRefName"], "issue-719-preserve-health-endpoint-transport-and-correct-korad-operation-identity")
        self.assertEqual(len(requests), 1)
        self.assertEqual(requests[0].get_header("X-github-api-version"), "2022-11-28")

    def test_versioned_native_single_pr_readback_exposes_missing_merge_identity(self) -> None:
        fixture = load_contract_fixture(REST_CONTRACT_2026_DIR / "pull-2026-03-10.json")
        self.assertEqual(fixture["response_api_version_selected"], "2026-03-10")
        self.assertNotIn("merge_commit_sha", fixture["response"])

        api = self.api(lambda *_args: RECOVERY.HttpResponse(200, {}, json.dumps(fixture["response"]).encode("utf-8")))
        recovery = RECOVERY.PublicationAckRecovery(ROOT, REPOSITORY, api, FixtureRunner())
        result = recovery.readback_pr(720)

        self.assertIsNone(result["mergeCommit"])
        self.assertEqual(api.request_count, 1)


if __name__ == "__main__":
    unittest.main()
