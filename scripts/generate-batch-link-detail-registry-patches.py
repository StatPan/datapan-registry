#!/usr/bin/env python3
"""Generate adapter-safe link-detail patches for one materialization batch."""

from __future__ import annotations

import argparse
import datetime as dt
import html
import hashlib
import json
import pathlib
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from html.parser import HTMLParser
from datetime import datetime, timezone
from typing import Any


SCHEMA_VERSION = "datapan.link-detail-registry-patches.v1"
ANCHOR_RE = re.compile(r"(?is)<a\b[^>]*>")
HREF_RE = re.compile(r"(?is)\bhref\s*=\s*[\"']([^\"']+)[\"']")
CURRENT_LINK_CALL_RE = re.compile(r"^\s*fn_goUrlLink\s*\(\s*(['\"])([0-9]+)\1\s*\)\s*;?\s*$")
CURRENT_LINK_MARKER_RE = re.compile(r"(?<![A-Za-z0-9_$])fn_goUrlLink(?![A-Za-z0-9_$])")
CURRENT_HIDDEN_ID_FIELDS = {"publicDataPk", "publicDataDetailPk"}
CURRENT_DETAIL_PAGE_URL = "https://www.data.go.kr/data/{dataset_id}/openapi.do"
CURRENT_LINK_RESOLVER_URL = "https://www.data.go.kr/tcs/dss/selectApiLinkUrl.do?publicDataPk={dataset_id}"
MAX_LINK_RESOLVER_RESPONSE_BYTES = 64 * 1024
MAX_RESOLVED_LINK_URL_CHARS = 4096
CURRENT_LINK_RESOLVER_METHOD = "data_go_kr_select_api_link_url_v1"
SECRET_LINK_QUERY_KEYS = {
    # Keys are compared after case folding and punctuation removal below.
    # Keep this local to resolved-link metadata: URLs may be retained in
    # receipts, so common credential-bearing query names must never survive.
    "key", "apikey", "servicekey", "token", "accesstoken", "authorization",
    "signature", "password", "passwd", "passphrase", "clientsecret",
    "refreshtoken", "secret", "apisecret", "credential", "queryvalue",
}


class LinkDetailContractError(ValueError):
    """A fixed, safe parser/resolver contract rejection."""


class _CurrentTemplateParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.hidden_values: dict[str, list[tuple[str, str]]] = {name: [] for name in CURRENT_HIDDEN_ID_FIELDS}
        self.shortcuts: list[str] = []
        self.invalid_target_attribute = False

    @staticmethod
    def _attribute_values(attributes: list[tuple[str, str | None]], name: str) -> list[str | None]:
        return [value for key, value in attributes if key.lower() == name.lower()]

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() == "input":
            ids = self._attribute_values(attrs, "id")
            target_ids = [value for value in ids if value in CURRENT_HIDDEN_ID_FIELDS]
            if not target_ids:
                return
            if len(ids) != 1 or len(target_ids) != 1:
                self.invalid_target_attribute = True
                return
            field = str(target_ids[0])
            types = self._attribute_values(attrs, "type")
            values = self._attribute_values(attrs, "value")
            if len(types) != 1 or str(types[0] or "").lower() != "hidden" or len(values) != 1:
                self.hidden_values[field].append(("invalid", ""))
                return
            value = str(values[0] or "").strip()
            self.hidden_values[field].append(("hidden", value))
            return
        if tag.lower() != "button":
            return
        onclicks = self._attribute_values(attrs, "onclick")
        if not onclicks:
            return
        if len(onclicks) != 1:
            if any(CURRENT_LINK_MARKER_RE.search(str(value or "")) for value in onclicks):
                self.shortcuts.append("invalid")
            return
        onclick = str(onclicks[0] or "")
        if not CURRENT_LINK_MARKER_RE.search(onclick):
            return
        match = CURRENT_LINK_CALL_RE.fullmatch(onclick)
        self.shortcuts.append(match.group(2) if match else "invalid")


def extract_current_template_dataset_id(page_html: str, expected_id: str) -> dict[str, str | None] | None:
    """Recognize only the bound current data.go.kr record shortcut.

    Returns ``None`` when the page has no ``button`` invoking the known helper.
    A page that attempts the helper but is malformed or bound to another row is
    rejected rather than treated as a page with no link.
    """
    if not isinstance(expected_id, str) or not re.fullmatch(r"[0-9]+", expected_id):
        raise LinkDetailContractError("current_template_candidate_id_invalid")
    if not isinstance(page_html, str):
        raise LinkDetailContractError("current_template_page_invalid")
    parser = _CurrentTemplateParser()
    try:
        parser.feed(page_html)
        parser.close()
    except (ValueError, AssertionError) as exc:
        raise LinkDetailContractError("current_template_page_parse_invalid") from exc
    if not parser.shortcuts:
        return None
    if parser.invalid_target_attribute or len(parser.shortcuts) != 1 or parser.shortcuts[0] == "invalid":
        raise LinkDetailContractError("current_template_shortcut_ambiguous")
    shortcut_id = parser.shortcuts[0]
    if shortcut_id != expected_id:
        raise LinkDetailContractError("current_template_shortcut_id_mismatch")
    public_ids = parser.hidden_values["publicDataPk"]
    if len(public_ids) != 1 or public_ids[0][0] != "hidden":
        raise LinkDetailContractError("current_template_public_data_pk_ambiguous")
    public_data_pk = public_ids[0][1]
    if not re.fullmatch(r"[0-9]+", public_data_pk) or public_data_pk != expected_id:
        raise LinkDetailContractError("current_template_public_data_pk_mismatch")
    detail_ids = parser.hidden_values["publicDataDetailPk"]
    if len(detail_ids) > 1 or (detail_ids and detail_ids[0][0] != "hidden"):
        raise LinkDetailContractError("current_template_detail_pk_ambiguous")
    public_data_detail_pk = detail_ids[0][1] if detail_ids else None
    if public_data_detail_pk is not None and (
        not public_data_detail_pk or len(public_data_detail_pk) > 256
        or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9:._-]*", public_data_detail_pk)
    ):
        raise LinkDetailContractError("current_template_detail_pk_invalid")
    return {
        "dataset_id": expected_id,
        "public_data_pk": public_data_pk,
        "public_data_detail_pk": public_data_detail_pk,
    }


def _strict_json_object(raw: bytes) -> dict[str, Any]:
    if not isinstance(raw, bytes) or not raw or len(raw) > MAX_LINK_RESOLVER_RESPONSE_BYTES:
        raise LinkDetailContractError("link_resolver_response_size_invalid")

    def object_no_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise LinkDetailContractError("link_resolver_json_duplicate_key")
            result[key] = value
        return result

    try:
        value = json.loads(
            raw.decode("utf-8", errors="strict"),
            object_pairs_hook=object_no_duplicates,
            parse_constant=lambda _value: (_ for _ in ()).throw(LinkDetailContractError("link_resolver_json_invalid")),
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise LinkDetailContractError("link_resolver_json_invalid") from exc
    if not isinstance(value, dict):
        raise LinkDetailContractError("link_resolver_shape_invalid")
    return value


def _normalized_link_query_key(value: str) -> str:
    """Canonicalize encoded query names before checking for secret fields."""
    decoded = value
    for _ in range(4):
        if re.search(r"%(?![0-9a-fA-F]{2})", decoded):
            raise LinkDetailContractError("link_resolver_url_invalid")
        next_decoded = urllib.parse.unquote_plus(decoded, errors="strict")
        if next_decoded == decoded:
            break
        decoded = next_decoded
    # Do not accept multiply-encoded query names whose meaning depends on a
    # downstream decoder applying more layers than this validator.
    if re.search(r"%[0-9a-fA-F]{2}", decoded):
        raise LinkDetailContractError("link_resolver_url_unsafe")
    return re.sub(r"[^a-z0-9]", "", decoded.casefold())


def _validated_resolved_url(value: Any) -> str:
    if (
        not isinstance(value, str) or not value or len(value) > MAX_RESOLVED_LINK_URL_CHARS
        or value != value.strip() or any(ord(char) < 0x20 or ord(char) == 0x7F for char in value)
        or "#" in value
    ):
        raise LinkDetailContractError("link_resolver_url_invalid")
    try:
        parsed = urllib.parse.urlsplit(value)
        hostname = parsed.hostname
        # Accessing port validates malformed/out-of-range authority syntax.
        _port = parsed.port
    except ValueError as exc:
        raise LinkDetailContractError("link_resolver_url_invalid") from exc
    if (
        parsed.scheme.lower() not in {"http", "https"} or not hostname
        or parsed.username is not None or parsed.password is not None
        or hostname.lower() in {"data.go.kr", "www.data.go.kr", "localhost"}
    ):
        raise LinkDetailContractError("link_resolver_url_unsafe")
    try:
        query_keys = {
            _normalized_link_query_key(item.partition("=")[0])
            for item in parsed.query.split("&") if item
        }
    except LinkDetailContractError:
        raise
    except (UnicodeDecodeError, ValueError) as exc:
        raise LinkDetailContractError("link_resolver_url_invalid") from exc
    if query_keys.intersection(SECRET_LINK_QUERY_KEYS):
        raise LinkDetailContractError("link_resolver_url_unsafe")
    return value


def validate_link_resolver_response(
    raw_bytes: bytes,
    expected_id: str,
    observed_at: str,
    *,
    expected_public_data_detail_pk: str | None = None,
) -> dict[str, Any]:
    """Validate the exact fixed-route JSON response without promoting its URL."""
    if not isinstance(expected_id, str) or not re.fullmatch(r"[0-9]+", expected_id):
        raise LinkDetailContractError("link_resolver_dataset_id_invalid")
    try:
        observed = dt.datetime.fromisoformat(str(observed_at).replace("Z", "+00:00"))
    except ValueError as exc:
        raise LinkDetailContractError("link_resolver_observation_time_invalid") from exc
    if observed.tzinfo is None:
        raise LinkDetailContractError("link_resolver_observation_time_invalid")
    payload = _strict_json_object(raw_bytes)
    allowed_keys = {"status", "linkUrl", "publicDataDetailPk", "publicDataPk"}
    if set(payload) - allowed_keys or payload.get("status") is not True:
        raise LinkDetailContractError("link_resolver_status_or_shape_invalid")
    if "publicDataPk" in payload and payload.get("publicDataPk") != expected_id:
        raise LinkDetailContractError("link_resolver_dataset_echo_mismatch")
    echoed_detail = payload.get("publicDataDetailPk")
    if "publicDataDetailPk" in payload:
        if (
            not isinstance(echoed_detail, str) or not echoed_detail or len(echoed_detail) > 256
            or expected_public_data_detail_pk is None
            or echoed_detail != expected_public_data_detail_pk
        ):
            raise LinkDetailContractError("link_resolver_detail_echo_mismatch")
    resolved_url = _validated_resolved_url(payload.get("linkUrl"))
    return {
        "link_url": resolved_url,
        "link_url_sha256": hashlib.sha256(resolved_url.encode("utf-8")).hexdigest(),
        "response_sha256": hashlib.sha256(raw_bytes).hexdigest(),
        "response_bytes": len(raw_bytes),
        "observed_at": observed_at,
        "public_data_detail_pk": echoed_detail,
        "public_data_pk": expected_id if "publicDataPk" in payload else None,
    }


def validate_link_metadata(
    value: Any,
    expected_id: str,
    registered_adapter_hosts: set[str],
) -> dict[str, Any]:
    """Validate the bounded page→resolver chain retained on an unresolved row."""
    expected_fields = {
        "method", "dataset_id", "public_data_pk", "public_data_detail_pk", "page", "resolver",
    }
    if (
        not isinstance(value, dict) or set(value) != expected_fields
        or value.get("method") != CURRENT_LINK_RESOLVER_METHOD
        or not isinstance(expected_id, str) or not re.fullmatch(r"[0-9]+", expected_id)
        or value.get("dataset_id") != expected_id or value.get("public_data_pk") != expected_id
    ):
        raise LinkDetailContractError("link_metadata_identity_invalid")
    page = value.get("page")
    expected_page_fields = {"url", "effective_url", "sha256", "bytes", "observed_at"}
    expected_page_url = CURRENT_DETAIL_PAGE_URL.format(dataset_id=expected_id)
    if (
        not isinstance(page, dict) or set(page) != expected_page_fields
        or page.get("url") != expected_page_url or page.get("effective_url") != expected_page_url
        or not isinstance(page.get("sha256"), str) or not re.fullmatch(r"[a-f0-9]{64}", page["sha256"])
        or not isinstance(page.get("bytes"), int) or isinstance(page.get("bytes"), bool)
        or not 1 <= page["bytes"] <= 1024 * 1024
    ):
        raise LinkDetailContractError("link_metadata_page_invalid")
    page_detail_pk = value.get("public_data_detail_pk")
    if page_detail_pk is not None and (
        not isinstance(page_detail_pk, str) or not page_detail_pk or len(page_detail_pk) > 256
        or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9:._-]*", page_detail_pk)
    ):
        raise LinkDetailContractError("link_metadata_page_identity_invalid")
    resolver = value.get("resolver")
    expected_resolver_fields = {
        "request_url", "effective_url", "sha256", "bytes", "observed_at",
        "public_data_detail_pk", "resolved_url", "resolved_url_sha256",
    }
    expected_resolver_url = CURRENT_LINK_RESOLVER_URL.format(dataset_id=expected_id)
    if (
        not isinstance(resolver, dict) or set(resolver) != expected_resolver_fields
        or resolver.get("request_url") != expected_resolver_url
        or resolver.get("effective_url") != expected_resolver_url
        or not isinstance(resolver.get("sha256"), str) or not re.fullmatch(r"[a-f0-9]{64}", resolver["sha256"])
        or not isinstance(resolver.get("bytes"), int) or isinstance(resolver.get("bytes"), bool)
        or not 1 <= resolver["bytes"] <= MAX_LINK_RESOLVER_RESPONSE_BYTES
        or not isinstance(resolver.get("resolved_url_sha256"), str)
        or resolver.get("resolved_url_sha256") != hashlib.sha256(str(resolver.get("resolved_url") or "").encode("utf-8")).hexdigest()
    ):
        raise LinkDetailContractError("link_metadata_resolver_invalid")
    echoed_detail = resolver.get("public_data_detail_pk")
    if echoed_detail is not None and (
        not isinstance(echoed_detail, str) or page_detail_pk is None or echoed_detail != page_detail_pk
    ):
        raise LinkDetailContractError("link_metadata_echo_binding_invalid")
    resolved_url = _validated_resolved_url(resolver.get("resolved_url"))
    try:
        host = (urllib.parse.urlsplit(resolved_url).hostname or "").lower()
    except ValueError as exc:
        raise LinkDetailContractError("link_metadata_resolved_url_invalid") from exc
    hosts = {str(item).strip().lower() for item in registered_adapter_hosts}
    if host not in hosts:
        raise LinkDetailContractError("link_metadata_resolved_host_unregistered")
    try:
        page_time = dt.datetime.fromisoformat(str(page.get("observed_at")).replace("Z", "+00:00"))
        resolver_time = dt.datetime.fromisoformat(str(resolver.get("observed_at")).replace("Z", "+00:00"))
    except ValueError as exc:
        raise LinkDetailContractError("link_metadata_time_invalid") from exc
    if (
        page_time.tzinfo is None or resolver_time.tzinfo is None
        or resolver_time < page_time
        or resolver_time - page_time > dt.timedelta(minutes=10)
    ):
        raise LinkDetailContractError("link_metadata_time_order_invalid")
    return value


def load_json(path: pathlib.Path) -> Any:
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def write_json(path: pathlib.Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        json.dump(value, handle, ensure_ascii=False, indent=2)
        handle.write("\n")


def registered_hosts(provider_index: dict[str, Any]) -> set[str]:
    out: set[str] = set()
    for adapter in provider_index.get("adapters") or []:
        for host in adapter.get("hosts") or []:
            host = str(host).strip().lower()
            if host:
                out.add(host)
    return out


def data_go_kr_application_url(dataset_id: str) -> str:
    return f"https://www.data.go.kr/data/{dataset_id}/openapi.do"


def fetch_text(url: str, timeout: float) -> str:
    request = urllib.request.Request(url, headers={"User-Agent": "datapan-registry-link-detail-batch/1.0"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.read(2 * 1024 * 1024).decode("utf-8", errors="replace")


def extract_link_detail_operation_urls(page_html: str) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for tag in ANCHOR_RE.findall(page_html):
        if "fn_LinkApiRequest" not in tag:
            continue
        match = HREF_RE.search(tag)
        if not match:
            continue
        raw = html.unescape(match.group(1)).strip()
        parsed = urllib.parse.urlparse(raw)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            continue
        if parsed.hostname.lower() in {"data.go.kr", "www.data.go.kr"}:
            continue
        if raw in seen:
            continue
        seen.add(raw)
        out.append(raw)
    return out


def operation_name(row: dict[str, Any], index: int, total: int) -> str:
    raw = ((row.get("source") or {}).get("raw") or {}) if isinstance(row.get("source"), dict) else {}
    name = str(raw.get("title") or row.get("title") or raw.get("list_title") or "").strip()
    if total <= 1:
        return name
    return f"{name} 외부 링크 {index + 1}"


def operation(row: dict[str, Any], endpoint: str, index: int, total: int) -> dict[str, Any]:
    raw = dict(((row.get("source") or {}).get("raw") or {}) if isinstance(row.get("source"), dict) else {})
    name = operation_name(row, index, total)
    raw["operation_nm"] = name
    raw["operation_url"] = endpoint
    return {
        "name": name,
        "endpoint": endpoint,
        "source": {
            "system": "data.go.kr",
            "url": str(raw.get("meta_url") or row.get("source", {}).get("url") or data_go_kr_application_url(str(row.get("id") or ""))),
            "raw": raw,
        },
    }


def operation_host(op: dict[str, Any]) -> str:
    return (urllib.parse.urlparse(str(op.get("endpoint") or "")).hostname or "").lower()


def batch_apis(batch: dict[str, Any]) -> list[dict[str, Any]]:
    apis = batch.get("apis")
    if not isinstance(apis, list):
        raise ValueError("batch.apis must be an array")
    return [api for api in apis if isinstance(api, dict)]


def build_report(
    registry: list[Any],
    batch: dict[str, Any],
    provider_index: dict[str, Any],
    *,
    limit: int,
    delay: float,
    timeout: float,
) -> dict[str, Any]:
    hosts = registered_hosts(provider_index)
    rows_by_id = {str(row.get("id") or ""): row for row in registry if isinstance(row, dict)}
    patches: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    failed: list[dict[str, Any]] = []
    fetched = 0
    for api in batch_apis(batch):
        if limit > 0 and fetched >= limit:
            skipped.append({"dataset_id": str(api.get("dataset_id") or ""), "reason": "limit_reached"})
            continue
        dataset_id = str(api.get("dataset_id") or "")
        row = rows_by_id.get(dataset_id)
        if not row:
            skipped.append({"dataset_id": dataset_id, "reason": "missing_registry_row"})
            continue
        if row.get("operations"):
            skipped.append({"dataset_id": dataset_id, "reason": "already_has_operations"})
            continue
        url = data_go_kr_application_url(dataset_id)
        try:
            body = fetch_text(url, timeout)
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            failed.append({"dataset_id": dataset_id, "reason": "fetch_failed", "message": str(exc)})
            continue
        fetched += 1
        links = extract_link_detail_operation_urls(body)
        if delay > 0:
            time.sleep(delay)
        if not links:
            skipped.append({"dataset_id": dataset_id, "reason": "missing_link_detail_operations"})
            continue
        operations = [operation(row, link, index, len(links)) for index, link in enumerate(links)]
        operation_hosts = [operation_host(op) for op in operations]
        missing_hosts = sorted({host for host in operation_hosts if host not in hosts})
        if missing_hosts:
            skipped.append(
                {
                    "dataset_id": dataset_id,
                    "title": row.get("title"),
                    "organization": row.get("organization"),
                    "operation_count": len(operations),
                    "reason": "unregistered_adapter_host",
                    "hosts": missing_hosts,
                }
            )
            continue
        patches.append(
            {
                "dataset_id": dataset_id,
                "title": row.get("title"),
                "organization": row.get("organization"),
                "action": "replace_empty_operations",
                "operation_count": len(operations),
                "hosts": sorted(set(operation_hosts)),
                "operations": operations,
            }
        )
    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        "summary": {
            "batch_label": batch.get("label"),
            "organization": batch.get("organization"),
            "input_apis": len(batch_apis(batch)),
            "details_fetched": fetched,
            "patches": len(patches),
            "operations_to_add": sum(int(patch["operation_count"]) for patch in patches),
            "skipped": len(skipped),
            "failed": len(failed),
            "registered_hosts": len(hosts),
        },
        "patches": patches,
        "skipped": skipped,
        "failed": failed,
    }


def write_markdown(path: pathlib.Path, report: dict[str, Any]) -> None:
    summary = report["summary"]
    lines = [
        "# data.go.kr Batch Link Detail Registry Patches",
        "",
        "This report fetches one operation-materialization batch from public data.go.kr detail pages and keeps only operations whose hosts already have registered adapters.",
        "",
        f"- Generated at: `{report['generated_at']}`",
        f"- Batch: `{summary.get('batch_label')}`",
        f"- Organization: `{summary.get('organization')}`",
        f"- Input APIs: `{summary['input_apis']}`",
        f"- Details fetched: `{summary['details_fetched']}`",
        f"- Patches: `{summary['patches']}`",
        f"- Operations to add: `{summary['operations_to_add']}`",
        f"- Skipped: `{summary['skipped']}`",
        f"- Failed: `{summary['failed']}`",
        "",
        "## Patches",
        "",
        "| Dataset ID | Organization | Title | Operations | Hosts |",
        "| --- | --- | --- | ---: | --- |",
    ]
    for patch in report["patches"]:
        lines.append(
            f"| {patch['dataset_id']} | {patch.get('organization') or ''} | {patch.get('title') or ''} | {patch['operation_count']} | {', '.join(patch.get('hosts') or [])} |"
        )
    lines.extend(["", "## Skipped Reasons", ""])
    counts: dict[str, int] = {}
    for row in report["skipped"]:
        reason = str(row.get("reason") or "unknown")
        counts[reason] = counts.get(reason, 0) + 1
    for reason, count in sorted(counts.items()):
        lines.append(f"- `{reason}`: `{count}`")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--registry", default="data/data-go-kr.registry.json", type=pathlib.Path)
    parser.add_argument("--batch", required=True, type=pathlib.Path)
    parser.add_argument("--provider-index", default="data/provider-index.json", type=pathlib.Path)
    parser.add_argument("--limit", type=int, default=100)
    parser.add_argument("--delay", type=float, default=0.05)
    parser.add_argument("--timeout", type=float, default=20)
    parser.add_argument("--output", default="reports/data-go-kr/link-detail-registry-patches.json", type=pathlib.Path)
    parser.add_argument("--markdown-output", default="docs/data-go-kr-link-detail-registry-patches.md", type=pathlib.Path)
    args = parser.parse_args()
    report = build_report(
        load_json(args.registry),
        load_json(args.batch),
        load_json(args.provider_index),
        limit=args.limit,
        delay=args.delay,
        timeout=args.timeout,
    )
    write_json(args.output, report)
    write_markdown(args.markdown_output, report)
    summary = report["summary"]
    print(
        f"wrote {args.output} and {args.markdown_output} "
        f"(patches={summary['patches']}, operations={summary['operations_to_add']}, skipped={summary['skipped']}, failed={summary['failed']})"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
