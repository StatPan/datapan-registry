#!/usr/bin/env python3
"""Generate the source-scoped report inventory."""

from __future__ import annotations

import argparse
import hashlib
import json
import pathlib
from typing import Any


SCHEMA_VERSION = "datapan.source-report-inventory.v1"
SOURCE_PROFILES_GLOB = "sources/*.json"
REPORTS_ROOT = pathlib.Path("reports")
SCHEMA_INDEX = pathlib.Path("schemas/index.json")
DEFAULT_OUTPUT = REPORTS_ROOT / "source-report-inventory.json"

RECOMMENDED_REPORTS = [
    "adapter-targets.json",
    "catalog-audit.json",
    "catalog-diff.json",
    "coverage.json",
    "dependencies.json",
    "error-catalog.json",
    "latest-verification.json",
    "latest-verification-summary.json",
    "provider-backlog.json",
    "route-disposition.json",
    "runtime-candidates.json",
    "runtime-evidence-plan.json",
    "verification-plan.json",
]

SOURCE_REPORT_ALIASES = {
    # These data.go.kr reports are still served from legacy top-level paths for
    # compatibility, but they are source-owned release evidence.
    "data_go_kr": [
        "adapter-targets.json",
        "catalog-audit.json",
        "catalog-diff.json",
        "coverage.json",
        "dependencies.json",
        "error-catalog.json",
        "latest-verification-summary.json",
        "latest-verification.json",
        "provider-backlog.json",
        "route-disposition.json",
        "verification-plan.json",
    ],
}


def load_json(path: pathlib.Path) -> Any:
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def write_json(path: pathlib.Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        json.dump(value, handle, ensure_ascii=False, indent=2)
        handle.write("\n")


def as_dict(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be an object")
    return value


def portable_path(path: pathlib.Path) -> str:
    return path.as_posix()


def file_digest(path: pathlib.Path) -> tuple[int, str]:
    data = path.read_bytes()
    return len(data), hashlib.sha256(data).hexdigest()


def percent(part: int, whole: int) -> float:
    if whole <= 0:
        return 0.0
    return round((part / whole) * 100, 1)


def report_dir_name(source_id: str) -> str:
    return source_id.replace("_", "-")


def schema_uri(schema_version: str) -> str:
    return f"https://schemas.datapan.dev/{schema_version}.schema.json"


def schema_path(schema_version: str) -> str:
    return f"schemas/{schema_version}.schema.json"


def indexed_schema_ids(schema_index_path: pathlib.Path = SCHEMA_INDEX) -> set[str]:
    schema_index = as_dict(load_json(schema_index_path), str(schema_index_path))
    schemas = schema_index.get("schemas")
    if not isinstance(schemas, list):
        raise ValueError("schemas/index.json schemas must be an array")
    ids: set[str] = set()
    for item in schemas:
        if not isinstance(item, dict):
            continue
        schema_id = item.get("id")
        if isinstance(schema_id, str) and schema_id:
            ids.add(schema_id)
        path = item.get("path")
        if isinstance(path, str) and path.startswith("schemas/") and path.endswith(".schema.json"):
            version = path.removeprefix("schemas/").removesuffix(".schema.json")
            ids.add(schema_uri(version))
    return ids


def schema_index_input(schema_index_path: pathlib.Path = SCHEMA_INDEX) -> dict[str, Any]:
    schema_index = as_dict(load_json(schema_index_path), str(schema_index_path))
    schemas = schema_index.get("schemas")
    if not isinstance(schemas, list):
        raise ValueError("schemas/index.json schemas must be an array")
    byte_count, sha256 = file_digest(schema_index_path)
    return {
        "path": portable_path(schema_index_path),
        "schemas": len(schemas),
        "bytes": byte_count,
        "sha256": sha256,
    }


def source_profile_input(profile_path: pathlib.Path, profile: dict[str, Any]) -> dict[str, Any]:
    source_id = profile.get("source_id")
    provider = profile.get("provider")
    if not isinstance(source_id, str) or not source_id:
        raise ValueError(f"{profile_path}.source_id must be a non-empty string")
    if not isinstance(provider, str) or not provider:
        raise ValueError(f"{profile_path}.provider must be a non-empty string")
    byte_count, sha256 = file_digest(profile_path)
    return {
        "path": portable_path(profile_path),
        "source_id": source_id,
        "provider": provider,
        "bytes": byte_count,
        "sha256": sha256,
    }


def report_entry(path: pathlib.Path, schema_ids: set[str]) -> dict[str, Any]:
    byte_count, sha256 = file_digest(path)
    entry: dict[str, Any] = {
        "name": path.name,
        "path": portable_path(path),
        "bytes": byte_count,
        "sha256": sha256,
    }
    try:
        payload = load_json(path)
    except json.JSONDecodeError:
        return entry
    if isinstance(payload, dict) and isinstance(payload.get("schema_version"), str):
        schema_version = payload["schema_version"]
        expected_schema_id = schema_uri(schema_version)
        entry["schema_version"] = schema_version
        entry["expected_schema_id"] = expected_schema_id
        entry["expected_schema_path"] = schema_path(schema_version)
        entry["schema_indexed"] = expected_schema_id in schema_ids
    return entry


def source_report_paths(source_id: str, report_dir: pathlib.Path) -> tuple[list[pathlib.Path], list[pathlib.Path]]:
    report_paths = sorted(report_dir.glob("*.json")) if report_dir.exists() else []
    alias_paths = [
        REPORTS_ROOT / name
        for name in SOURCE_REPORT_ALIASES.get(source_id, [])
        if (REPORTS_ROOT / name).is_file()
    ]
    existing = {path.as_posix() for path in report_paths}
    alias_paths = [path for path in alias_paths if path.as_posix() not in existing]
    return sorted(report_paths + alias_paths), sorted(alias_paths)


def generated_at_from_reports(report_paths: list[pathlib.Path]) -> str:
    values: list[str] = []
    for path in report_paths:
        try:
            payload = load_json(path)
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict) and isinstance(payload.get("generated_at"), str):
            values.append(payload["generated_at"])
    if values:
        return sorted(values)[-1]
    return "1970-01-01T00:00:00Z"


def source_profiles() -> list[tuple[pathlib.Path, dict[str, Any]]]:
    profiles: list[tuple[pathlib.Path, dict[str, Any]]] = []
    for path in sorted(pathlib.Path("sources").glob("*.json")):
        profile = as_dict(load_json(path), str(path))
        profiles.append((path, profile))
    return sorted(profiles, key=lambda item: str(item[1].get("source_id")))


def build_report() -> dict[str, Any]:
    recommended = sorted(RECOMMENDED_REPORTS)
    schema_ids = indexed_schema_ids()
    profiles = source_profiles()
    source_inputs = [source_profile_input(path, profile) for path, profile in profiles]
    sources: list[dict[str, Any]] = []
    all_report_paths: list[pathlib.Path] = []
    present_recommended_total = 0
    missing_recommended_total = 0
    source_report_dirs = 0
    schema_backed_total = 0
    schema_indexed_total = 0

    for profile_path, profile in profiles:
        source_id = str(profile.get("source_id"))
        report_dir = REPORTS_ROOT / report_dir_name(source_id)
        report_paths, alias_paths = source_report_paths(source_id, report_dir)
        all_report_paths.extend(report_paths)
        if report_dir.exists():
            source_report_dirs += 1

        present_names = sorted(path.name for path in report_paths)
        present_reports = [report_entry(path, schema_ids) for path in report_paths]
        schema_backed_total += sum(1 for entry in present_reports if "schema_version" in entry)
        schema_indexed_total += sum(1 for entry in present_reports if entry.get("schema_indexed") is True)
        present_recommended = [name for name in recommended if name in present_names]
        missing_recommended = [name for name in recommended if name not in present_names]
        extra_reports = [name for name in present_names if name not in recommended]
        present_recommended_total += len(present_recommended)
        missing_recommended_total += len(missing_recommended)

        sources.append(
            {
                "source_id": source_id,
                "provider": profile.get("provider"),
                "display_name": profile.get("display_name"),
                "source_profile": portable_path(profile_path),
                "report_dir": portable_path(report_dir),
                "report_alias_paths": [portable_path(path) for path in alias_paths],
                "report_dir_exists": report_dir.exists(),
                "report_count": len(report_paths),
                "present_reports": present_reports,
                "present_recommended_reports": present_recommended,
                "missing_recommended_reports": missing_recommended,
                "extra_reports": extra_reports,
                "recommended_report_coverage_percent": percent(len(present_recommended), len(recommended)),
            }
        )

    recommended_slots = len(sources) * len(recommended)
    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at": generated_at_from_reports(all_report_paths),
        "provider": "multi-source",
        "generation_inputs": {
            "source_profiles_glob": SOURCE_PROFILES_GLOB,
            "reports_root": portable_path(REPORTS_ROOT),
            "schema_index": portable_path(SCHEMA_INDEX),
        },
        "source_profile_inputs": source_inputs,
        "schema_index_input": schema_index_input(),
        "recommended_reports": recommended,
        "summary": {
            "sources": len(sources),
            "source_report_dirs": source_report_dirs,
            "report_total": len(all_report_paths),
            "recommended_report_slots": recommended_slots,
            "present_recommended_reports": present_recommended_total,
            "missing_recommended_reports": missing_recommended_total,
            "source_report_coverage_percent": percent(present_recommended_total, recommended_slots),
            "schema_backed_reports": schema_backed_total,
            "schema_indexed_reports": schema_indexed_total,
            "schema_missing_reports": schema_backed_total - schema_indexed_total,
        },
        "sources": sources,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", default=DEFAULT_OUTPUT, type=pathlib.Path)
    args = parser.parse_args()

    report = build_report()
    write_json(args.output, report)
    summary = report["summary"]
    print(
        f"wrote {args.output} "
        f"(sources={summary['sources']}, reports={summary['report_total']}, "
        f"coverage={summary['source_report_coverage_percent']}%)"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
