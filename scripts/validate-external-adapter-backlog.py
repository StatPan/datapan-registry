#!/usr/bin/env python3
"""Validate the generated data.go.kr external adapter backlog."""

from __future__ import annotations

import argparse
import json
import pathlib
import subprocess
import sys
import tempfile
from typing import Any

try:
    import jsonschema
except ImportError as exc:  # pragma: no cover - environment guard
    raise SystemExit("missing dependency: install jsonschema before validating external adapter backlog") from exc


EXPECTED_SCHEMA_VERSION = "datapan.external-adapter-backlog.v1"
DEFAULT_SCHEMA = pathlib.Path("schemas/datapan.external-adapter-backlog.v1.schema.json")


def load_json(path: pathlib.Path) -> Any:
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def as_dict(value: Any, path: pathlib.Path | str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"{path} must contain a JSON object")
    return value


def as_list(value: Any, path: pathlib.Path | str) -> list[Any]:
    if not isinstance(value, list):
        raise ValueError(f"{path} must contain an array")
    return value


def normalize_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def referenced_path(report: dict[str, Any], key: str) -> pathlib.Path:
    inputs = as_dict(report.get("generation_inputs"), "generation_inputs")
    raw_path = inputs.get(key)
    if not isinstance(raw_path, str) or not raw_path:
        raise ValueError(f"generation_inputs.{key} must be a non-empty path")
    path = pathlib.Path(raw_path)
    if not path.exists():
        raise ValueError(f"generation_inputs.{key} does not exist: {path}")
    return path


def validate_schema(report: dict[str, Any], schema_path: pathlib.Path) -> None:
    schema = load_json(schema_path)
    validator = jsonschema.Draft202012Validator(schema)
    errors = sorted(validator.iter_errors(report), key=lambda error: list(error.path))
    if errors:
        messages = []
        for error in errors:
            location = ".".join(str(part) for part in error.path) or "<root>"
            messages.append(f"{location}: {error.message}")
        raise ValueError("; ".join(messages))


def validate_report(
    report_path: pathlib.Path,
    markdown_path: pathlib.Path,
    generator: pathlib.Path,
    schema_path: pathlib.Path,
) -> None:
    report = as_dict(load_json(report_path), report_path)
    if report.get("schema_version") != EXPECTED_SCHEMA_VERSION:
        raise ValueError(
            f"schema_version expected {EXPECTED_SCHEMA_VERSION}, got {report.get('schema_version')}"
        )
    validate_schema(report, schema_path)

    route_disposition = as_dict(load_json(referenced_path(report, "route_disposition")), "route_disposition")
    coverage = as_dict(load_json(referenced_path(report, "coverage")), "coverage")
    adapter_targets = as_dict(load_json(referenced_path(report, "adapter_targets")), "adapter_targets")
    route_summary = as_dict(route_disposition.get("summary"), "route_disposition.summary")
    coverage_summary = as_dict(coverage.get("summary"), "coverage.summary")
    adapter_summary = as_dict(adapter_targets.get("summary"), "adapter_targets.summary")

    summary = as_dict(report.get("summary"), "report.summary")
    hosts = [as_dict(item, "report.hosts[]") for item in as_list(report.get("hosts"), "report.hosts")]
    candidate_routes = [
        as_dict(item, "report.candidate_routes[]")
        for item in as_list(report.get("candidate_routes"), "report.candidate_routes")
    ]
    excluded = as_dict(report.get("excluded_dispositions"), "report.excluded_dispositions")
    dead_routes = as_list(excluded.get("dead_route_candidate"), "report.excluded_dispositions.dead_route_candidate")
    transient_routes = as_list(excluded.get("transient_failure"), "report.excluded_dispositions.transient_failure")

    expected_candidates = int(route_summary.get("adapter_candidates") or 0)
    if summary.get("candidate_operations") != expected_candidates:
        raise ValueError(
            f"summary.candidate_operations expected {expected_candidates}, got {summary.get('candidate_operations')}"
        )
    if len(candidate_routes) != expected_candidates:
        raise ValueError("candidate_routes length does not match route-disposition adapter_candidates")
    if any(route.get("disposition") != "adapter_candidate" for route in candidate_routes):
        raise ValueError("candidate_routes must contain only adapter_candidate routes")
    if summary.get("unclassified_missing_routes") != route_summary.get("without_probe_evidence"):
        raise ValueError("summary.unclassified_missing_routes does not match route disposition")
    if summary.get("unclassified_missing_routes") != 0:
        raise ValueError("external adapter backlog is actionable only when unclassified_missing_routes is 0")
    if summary.get("raw_missing_adapter_operations") != coverage_summary.get("missing_adapter_operations"):
        raise ValueError("summary.raw_missing_adapter_operations does not match coverage")
    if summary.get("raw_missing_adapter_hosts") != coverage_summary.get("missing_adapter_hosts"):
        raise ValueError("summary.raw_missing_adapter_hosts does not match coverage")
    if summary.get("adapter_target_operations") != adapter_summary.get("target_operations"):
        raise ValueError("summary.adapter_target_operations does not match adapter targets")
    if summary.get("excluded_dead_route_candidates") != route_summary.get("dead_route_candidates"):
        raise ValueError("summary.excluded_dead_route_candidates does not match route disposition")
    if summary.get("excluded_transient_failures") != route_summary.get("transient_failures"):
        raise ValueError("summary.excluded_transient_failures does not match route disposition")
    if len(dead_routes) != int(route_summary.get("dead_route_candidates") or 0):
        raise ValueError("dead-route exclusion count does not match route disposition")
    if len(transient_routes) != int(route_summary.get("transient_failures") or 0):
        raise ValueError("transient-failure exclusion count does not match route disposition")

    host_counts: dict[str, int] = {}
    for route in candidate_routes:
        host = str(route.get("endpoint_host") or "")
        host_counts[host] = host_counts.get(host, 0) + 1
    report_host_counts = {
        str(host.get("host") or ""): int(host.get("candidate_operations") or 0)
        for host in hosts
    }
    if report_host_counts != host_counts:
        raise ValueError("host candidate counts do not match candidate routes")
    if summary.get("candidate_hosts") != len(host_counts):
        raise ValueError("summary.candidate_hosts does not match host count")

    with tempfile.TemporaryDirectory() as temp_dir:
        temp_json = pathlib.Path(temp_dir) / "external-adapter-backlog.json"
        temp_md = pathlib.Path(temp_dir) / "external-adapter-backlog.md"
        subprocess.run(
            [
                sys.executable,
                str(generator),
                "--output",
                str(temp_json),
                "--markdown-output",
                str(temp_md),
            ],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        expected = as_dict(load_json(temp_json), temp_json)
        expected_markdown = temp_md.read_text(encoding="utf-8")

    if normalize_json(report) != normalize_json(expected):
        raise ValueError("report is stale; regenerate with scripts/generate-external-adapter-backlog.py")
    if markdown_path.exists() and markdown_path.read_text(encoding="utf-8") != expected_markdown:
        raise ValueError(f"{markdown_path} is stale; regenerate with scripts/generate-external-adapter-backlog.py")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--generator",
        default="scripts/generate-external-adapter-backlog.py",
        type=pathlib.Path,
        help="external adapter backlog generator path",
    )
    parser.add_argument(
        "--markdown",
        default="docs/data-go-kr-external-adapter-backlog.md",
        type=pathlib.Path,
        help="generated markdown path",
    )
    parser.add_argument(
        "--schema",
        default=DEFAULT_SCHEMA,
        type=pathlib.Path,
        help="external adapter backlog JSON Schema path",
    )
    parser.add_argument(
        "report",
        nargs="?",
        default=pathlib.Path("reports/data-go-kr/external-adapter-backlog.json"),
        type=pathlib.Path,
        help="external adapter backlog report to validate",
    )
    args = parser.parse_args()

    try:
        validate_report(args.report, args.markdown, args.generator, args.schema)
    except Exception as exc:  # noqa: BLE001 - report all validation blockers
        print(f"FAIL {args.report}: {exc}", file=sys.stderr)
        return 1

    report = as_dict(load_json(args.report), args.report)
    summary = as_dict(report.get("summary"), "report.summary")
    print(
        f"ok {args.report} "
        f"(hosts={summary.get('candidate_hosts')}, operations={summary.get('candidate_operations')})"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
