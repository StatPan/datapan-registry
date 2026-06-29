#!/usr/bin/env python3
"""Validate checked-in source reference drift baseline reports."""

from __future__ import annotations

import argparse
import collections
import json
import pathlib
import sys

try:
    import jsonschema
except ImportError as exc:  # pragma: no cover - environment guard
    raise SystemExit(
        "missing dependency: install jsonschema before running source reference drift validation"
    ) from exc


REFERENCE_FIELDS = {
    "homepage_url",
    "api_docs_url",
    "key_request_url",
    "notice_url",
    "terms_url",
    "metadata_standard_url",
}


def load_json(path: pathlib.Path) -> object:
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def as_dict(value: object, path: pathlib.Path) -> dict[str, object]:
    if not isinstance(value, dict):
        raise ValueError(f"{path} must contain a JSON object")
    return value


def load_profiles(paths: list[pathlib.Path]) -> dict[str, dict[str, object]]:
    profiles: dict[str, dict[str, object]] = {}
    for profile_path in paths:
        profile = as_dict(load_json(profile_path), profile_path)
        source_id = profile.get("source_id")
        if not isinstance(source_id, str):
            raise ValueError(f"{profile_path} missing source_id")
        if source_id in profiles:
            raise ValueError(f"duplicate source profile source_id: {source_id}")
        profiles[source_id] = profile
    return profiles


def profile_reference_map(profile: dict[str, object]) -> dict[str, str]:
    references = as_dict(profile.get("references"), pathlib.Path("<profile>"))
    result: dict[str, str] = {}
    for key, value in references.items():
        if key in REFERENCE_FIELDS and isinstance(value, str):
            result[key] = value
    return result


def validate_consistency(
    report_path: pathlib.Path,
    report: dict[str, object],
    profiles: dict[str, dict[str, object]],
) -> None:
    sources = report.get("sources")
    if not isinstance(sources, list):
        raise ValueError("sources must be an array")

    report_sources: dict[str, dict[str, object]] = {}
    summary_counts: collections.Counter[str] = collections.Counter()
    reference_total = 0

    for index, raw_source in enumerate(sources):
        source = as_dict(raw_source, report_path)
        source_id = source.get("source_id")
        if not isinstance(source_id, str):
            raise ValueError(f"sources[{index}].source_id must be a string")
        if source_id in report_sources:
            raise ValueError(f"duplicate source entry: {source_id}")
        report_sources[source_id] = source

        profile = profiles.get(source_id)
        if profile is None:
            raise ValueError(f"sources[{index}] has no matching source profile: {source_id}")

        expected_profile_path = f"sources/{source_id}.json"
        if source_id == "data_go_kr":
            expected_profile_path = "sources/data_go_kr.json"
        if source.get("source_profile") != expected_profile_path:
            raise ValueError(
                f"{source_id}.source_profile expected {expected_profile_path}, got {source.get('source_profile')}"
            )
        if source.get("provider") != profile.get("provider"):
            raise ValueError(f"{source_id}.provider does not match source profile")

        profile_references = as_dict(profile.get("references"), pathlib.Path(source.get("source_profile", "<unknown>")))
        last_reviewed_at = profile_references.get("last_reviewed_at")
        if source.get("last_reviewed_at") != last_reviewed_at:
            raise ValueError(f"{source_id}.last_reviewed_at does not match source profile")

        expected_references = profile_reference_map(profile)
        raw_references = source.get("references")
        if not isinstance(raw_references, list):
            raise ValueError(f"{source_id}.references must be an array")

        actual_references: dict[str, str] = {}
        for reference_index, raw_reference in enumerate(raw_references):
            reference = as_dict(raw_reference, report_path)
            kind = reference.get("kind")
            url = reference.get("url")
            if not isinstance(kind, str) or not isinstance(url, str):
                raise ValueError(f"{source_id}.references[{reference_index}] must include kind and url")
            if kind in actual_references:
                raise ValueError(f"{source_id} duplicate reference kind: {kind}")
            actual_references[kind] = url

            if reference.get("reviewed_at") != last_reviewed_at:
                raise ValueError(f"{source_id}.{kind}.reviewed_at does not match source profile")

            check_mode = reference.get("check_mode")
            drift_status = reference.get("drift_status")
            if check_mode == "manual_review":
                summary_counts["manual_review_references"] += 1
            if check_mode == "network_fetch":
                summary_counts["network_checked_references"] += 1
            if drift_status in {"changed", "redirected"}:
                summary_counts["changed_references"] += 1
            if drift_status == "missing":
                summary_counts["missing_references"] += 1

            contract_impact = reference.get("contract_impact")
            if not isinstance(contract_impact, list):
                raise ValueError(f"{source_id}.{kind}.contract_impact must be an array")
            if drift_status in {"changed", "redirected", "missing"} and "unknown" not in contract_impact:
                summary_counts["contract_affecting_changes"] += 1

        if actual_references != expected_references:
            missing = sorted(set(expected_references) - set(actual_references))
            extra = sorted(set(actual_references) - set(expected_references))
            changed = sorted(
                key
                for key in set(expected_references).intersection(actual_references)
                if expected_references[key] != actual_references[key]
            )
            details = []
            if missing:
                details.append(f"missing={','.join(missing)}")
            if extra:
                details.append(f"extra={','.join(extra)}")
            if changed:
                details.append(f"changed={','.join(changed)}")
            raise ValueError(f"{source_id}.references do not match source profile ({'; '.join(details)})")

        reference_total += len(raw_references)

    if set(report_sources) != set(profiles):
        missing_sources = sorted(set(profiles) - set(report_sources))
        extra_sources = sorted(set(report_sources) - set(profiles))
        details = []
        if missing_sources:
            details.append(f"missing={','.join(missing_sources)}")
        if extra_sources:
            details.append(f"extra={','.join(extra_sources)}")
        raise ValueError(f"report sources do not match source profiles ({'; '.join(details)})")

    summary = as_dict(report.get("summary"), report_path)
    expected = {
        "sources": len(profiles),
        "references": reference_total,
        "manual_review_references": summary_counts["manual_review_references"],
        "network_checked_references": summary_counts["network_checked_references"],
        "changed_references": summary_counts["changed_references"],
        "missing_references": summary_counts["missing_references"],
        "contract_affecting_changes": summary_counts["contract_affecting_changes"],
    }
    for key, value in expected.items():
        if summary.get(key) != value:
            raise ValueError(f"summary.{key} expected {value}, got {summary.get(key)}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--schema",
        default="schemas/datapan.source-reference-drift.v1.schema.json",
        type=pathlib.Path,
        help="source reference drift JSON Schema path",
    )
    parser.add_argument(
        "--profiles",
        nargs="*",
        type=pathlib.Path,
        help="source profile files; defaults to sources/*.json",
    )
    parser.add_argument(
        "reports",
        nargs="*",
        type=pathlib.Path,
        help="reference drift reports to validate; defaults to reports/source-reference-drift.json",
    )
    args = parser.parse_args()

    reports = args.reports or [pathlib.Path("reports/source-reference-drift.json")]
    profiles = load_profiles(args.profiles or sorted(pathlib.Path("sources").glob("*.json")))
    schema = load_json(args.schema)
    validator = jsonschema.Draft202012Validator(schema)
    failures = 0

    for report_path in reports:
        try:
            instance = as_dict(load_json(report_path), report_path)
            errors = sorted(validator.iter_errors(instance), key=lambda error: list(error.path))
            if not errors:
                validate_consistency(report_path, instance, profiles)
        except Exception as exc:  # noqa: BLE001 - report all validation blockers
            print(f"FAIL {report_path}: {exc}", file=sys.stderr)
            failures += 1
            continue

        if errors:
            failures += 1
            print(f"FAIL {report_path}", file=sys.stderr)
            for error in errors:
                location = ".".join(str(part) for part in error.path) or "<root>"
                print(f"  {location}: {error.message}", file=sys.stderr)
            continue

        total_sources = instance.get("summary", {}).get("sources", "<unknown>")
        total_references = instance.get("summary", {}).get("references", "<unknown>")
        print(f"ok {report_path} (sources={total_sources}, references={total_references})")

    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
