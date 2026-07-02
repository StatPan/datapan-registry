#!/usr/bin/env python3
"""Generate the source-scoped report inventory."""

from __future__ import annotations

import argparse
import json
import pathlib
from typing import Any


SCHEMA_VERSION = "datapan.source-report-inventory.v1"
SOURCE_PROFILES_GLOB = "sources/*.json"
REPORTS_ROOT = pathlib.Path("reports")
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


def percent(part: int, whole: int) -> float:
    if whole <= 0:
        return 0.0
    return round((part / whole) * 100, 1)


def report_dir_name(source_id: str) -> str:
    return source_id.replace("_", "-")


def report_entry(path: pathlib.Path) -> dict[str, Any]:
    entry: dict[str, Any] = {
        "name": path.name,
        "path": portable_path(path),
    }
    try:
        payload = load_json(path)
    except json.JSONDecodeError:
        return entry
    if isinstance(payload, dict) and isinstance(payload.get("schema_version"), str):
        entry["schema_version"] = payload["schema_version"]
    return entry


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
    sources: list[dict[str, Any]] = []
    all_report_paths: list[pathlib.Path] = []
    present_recommended_total = 0
    missing_recommended_total = 0
    source_report_dirs = 0

    for profile_path, profile in source_profiles():
        source_id = str(profile.get("source_id"))
        report_dir = REPORTS_ROOT / report_dir_name(source_id)
        report_paths = sorted(report_dir.glob("*.json")) if report_dir.exists() else []
        all_report_paths.extend(report_paths)
        if report_dir.exists():
            source_report_dirs += 1

        present_names = sorted(path.name for path in report_paths)
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
                "report_dir_exists": report_dir.exists(),
                "report_count": len(report_paths),
                "present_reports": [report_entry(path) for path in report_paths],
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
        },
        "recommended_reports": recommended,
        "summary": {
            "sources": len(sources),
            "source_report_dirs": source_report_dirs,
            "report_total": len(all_report_paths),
            "recommended_report_slots": recommended_slots,
            "present_recommended_reports": present_recommended_total,
            "missing_recommended_reports": missing_recommended_total,
            "source_report_coverage_percent": percent(present_recommended_total, recommended_slots),
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
