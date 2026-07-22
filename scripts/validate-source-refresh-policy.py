#!/usr/bin/env python3
"""Validate source refresh policy and sustainable-coverage denominator completeness."""

from __future__ import annotations

import argparse
import json
import pathlib
import sys
import datetime as dt

try:
    import jsonschema
except ImportError as exc:
    raise SystemExit("missing dependency: install jsonschema") from exc


def load(path: pathlib.Path) -> object:
    return json.loads(path.read_text(encoding="utf-8"))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--policy", type=pathlib.Path, default=pathlib.Path("policy/source-refresh.json"))
    parser.add_argument("--schema", type=pathlib.Path, default=pathlib.Path("schemas/datapan.source-refresh-policy.v1.schema.json"))
    parser.add_argument("--coverage-policy", type=pathlib.Path, default=pathlib.Path("policy/sustainable-coverage.json"))
    parser.add_argument("--workflow", type=pathlib.Path, default=pathlib.Path(".github/workflows/upstream-catalog-refresh.yml"))
    args = parser.parse_args()
    try:
        policy, schema, coverage = load(args.policy), load(args.schema), load(args.coverage_policy)
        workflow_text = args.workflow.read_text(encoding="utf-8")
        jsonschema.Draft202012Validator(schema).validate(policy)
        assert isinstance(policy, dict) and isinstance(coverage, dict)
        sources = policy["sources"]
        ids = [item["source_id"] for item in sources]
        if len(ids) != len(set(ids)):
            raise ValueError("refresh source_id values must be unique")
        denominator_sources = [
            item for item in coverage.get("supported_sources", [])
            if item.get("catalog_scope") == "operation_denominator"
        ]
        denominators = {item["source_id"] for item in denominator_sources}
        aggregate_catalogs = {
            item["source_id"] for item in denominator_sources
            if load(pathlib.Path(item["coverage_report"]))["scope"]["kind"] == "aggregate_supported_catalog"
        }
        configured = set(ids)
        if configured != aggregate_catalogs:
            raise ValueError(
                "scheduled catalogue refresh sources must exactly match aggregate operation denominators; "
                f"missing={sorted(aggregate_catalogs-configured)} extra={sorted(configured-aggregate_catalogs)}"
            )
        for item in sources:
            profile = next(row["profile"] for row in coverage["supported_sources"] if row["source_id"] == item["source_id"])
            profile_value = load(pathlib.Path(profile))
            if not isinstance(profile_value, dict) or "catalogue_import" not in profile_value.get("adapter", {}).get("capabilities", []):
                raise ValueError(f"{item['source_id']} lacks catalogue_import capability")
            if item["publication"]["automatic"]:
                raise ValueError("scheduled refresh must never publish automatically")
            cron_marker = f'- cron: "{item["cadence"]["cron"]}"'
            if cron_marker not in workflow_text:
                raise ValueError(f"workflow schedule does not match {item['source_id']} cadence")
            observed = dt.datetime.fromisoformat(item["last_successful_observation"].replace("Z", "+00:00"))
            if observed > dt.datetime.now(dt.timezone.utc):
                raise ValueError(f"{item['source_id']} last_successful_observation is in the future")
            arguments = item["importer"]["arguments"]
            try:
                timeout_index = arguments.index("--timeout")
                timeout = arguments[timeout_index + 1]
            except (ValueError, IndexError) as exc:
                raise ValueError(f"{item['source_id']} must declare a bounded full-import --timeout") from exc
            if timeout != "10m":
                raise ValueError(f"{item['source_id']} full-import timeout must be the reviewed 10m budget, got {timeout!r}")
        required_workflow_fragments = (
            "run-upstream-refresh.py",
            "if: always()",
            "automatic publication:",
            "DATA_GO_KR_SERVICE_KEY: ${{ secrets.DATAPAN_DATA_GO_KR_SERVICE_KEY }}",
        )
        for fragment in required_workflow_fragments:
            if fragment not in workflow_text:
                raise ValueError(f"refresh workflow missing required fragment: {fragment}")
        print(f"ok {args.policy} (scheduled_catalogs={len(sources)}, operation_denominators={len(denominators)})")
        return 0
    except Exception as exc:  # noqa: BLE001
        print(f"FAIL source refresh policy: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
