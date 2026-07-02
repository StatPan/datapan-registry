#!/usr/bin/env python3
"""Validate adapter-safe link-detail registry patch reports."""

from __future__ import annotations

import json
import pathlib
from typing import Any
from urllib.parse import urlparse


EXPECTED_SCHEMA_VERSION = "datapan.link-detail-registry-patches.v1"
REPORT = pathlib.Path("reports/data-go-kr/link-detail-registry-patches.json")
MARKDOWN = pathlib.Path("docs/data-go-kr-link-detail-registry-patches.md")
PROVIDER_INDEX = pathlib.Path("data/provider-index.json")


def load_json(path: pathlib.Path) -> Any:
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def registered_hosts() -> set[str]:
    data = load_json(PROVIDER_INDEX)
    hosts: set[str] = set()
    for adapter in data.get("adapters") or []:
        for host in adapter.get("hosts") or []:
            host = str(host).strip().lower()
            if host:
                hosts.add(host)
    return hosts


def main() -> int:
    report = load_json(REPORT)
    if report.get("schema_version") != EXPECTED_SCHEMA_VERSION:
        raise ValueError(f"schema_version must be {EXPECTED_SCHEMA_VERSION}")
    if not MARKDOWN.exists():
        raise ValueError(f"{MARKDOWN} is missing")
    patches = report.get("patches")
    if not isinstance(patches, list) or not patches:
        raise ValueError("at least one patch is required")
    hosts = registered_hosts()
    seen: set[str] = set()
    operations = 0
    for index, patch in enumerate(patches):
        dataset_id = str(patch.get("dataset_id") or "")
        if not dataset_id:
            raise ValueError(f"patches[{index}].dataset_id must be non-empty")
        if dataset_id in seen:
            raise ValueError(f"duplicate dataset_id {dataset_id}")
        seen.add(dataset_id)
        if patch.get("action") != "replace_empty_operations":
            raise ValueError(f"patches[{index}].action must be replace_empty_operations")
        ops = patch.get("operations")
        if not isinstance(ops, list) or not ops:
            raise ValueError(f"patches[{index}].operations must be non-empty")
        if patch.get("operation_count") != len(ops):
            raise ValueError(f"patches[{index}].operation_count must match operations")
        operations += len(ops)
        for op_index, operation in enumerate(ops):
            endpoint = str(operation.get("endpoint") or "")
            host = (urlparse(endpoint).hostname or "").lower()
            if host not in hosts:
                raise ValueError(f"patches[{index}].operations[{op_index}] host is not registered: {host}")
            if not operation.get("name"):
                raise ValueError(f"patches[{index}].operations[{op_index}].name must be non-empty")
    summary = report.get("summary") or {}
    if summary.get("patches") != len(patches):
        raise ValueError("summary.patches must match patch count")
    if summary.get("operations_to_add") != operations:
        raise ValueError("summary.operations_to_add must match operation count")
    print(f"ok {REPORT} (patches={len(patches)}, operations={operations})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
