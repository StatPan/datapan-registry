from __future__ import annotations

import copy
import importlib.util
import json
import pathlib
import unittest
from datetime import datetime, timezone
from unittest import mock


SCRIPT = pathlib.Path(__file__).parents[1] / "scripts" / "generate-runtime-freshness-queue.py"
ROOT = SCRIPT.parents[1]
EXPECTED_POLICY_SOURCE_IDS = {
    "data_go_kr",
    "ecos",
    "kosis",
    "open_assembly",
    "seoul_open_data",
}
SPEC = importlib.util.spec_from_file_location("generate_runtime_freshness_queue", SCRIPT)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def policy_bound_operation_total(policy: dict, source_reports: dict[str, dict], rollup: dict) -> int:
    """Independently reconcile the five policy-bound denominator reports."""
    configured_sources = policy.get("supported_sources")
    if not isinstance(configured_sources, list):
        raise AssertionError("coverage policy must list supported sources")
    configured_ids = [row.get("source_id") for row in configured_sources if isinstance(row, dict)]
    if len(configured_sources) != len(EXPECTED_POLICY_SOURCE_IDS) or set(configured_ids) != EXPECTED_POLICY_SOURCE_IDS:
        raise AssertionError("coverage policy must contain each supported denominator source exactly once")
    if len(configured_ids) != len(set(configured_ids)):
        raise AssertionError("coverage policy contains duplicate source identities")
    if set(source_reports) != EXPECTED_POLICY_SOURCE_IDS:
        raise AssertionError("coverage report identities do not match the five policy sources")

    rollup_rows = rollup.get("sources")
    if not isinstance(rollup_rows, list):
        raise AssertionError("denominator rollup must list source contributions")
    rollup_by_id = {row.get("source_id"): row for row in rollup_rows if isinstance(row, dict)}
    if len(rollup_by_id) != len(rollup_rows) or set(rollup_by_id) != EXPECTED_POLICY_SOURCE_IDS:
        raise AssertionError("denominator rollup source identities do not match policy")

    operation_total = 0
    callable_total = 0
    for configured in configured_sources:
        source_id = configured["source_id"]
        coverage_path = configured.get("coverage_report")
        if configured.get("catalog_scope") != "operation_denominator" or not isinstance(coverage_path, str) or not coverage_path:
            raise AssertionError(f"policy denominator binding is invalid for {source_id}")
        report = source_reports.get(source_id)
        if not isinstance(report, dict) or report.get("source_id") != source_id:
            raise AssertionError(f"coverage report identity does not match policy for {source_id}")
        report_scope = report.get("scope")
        summary = report.get("summary")
        operations = report.get("operations")
        if not isinstance(report_scope, dict) or not isinstance(summary, dict) or not isinstance(operations, list):
            raise AssertionError(f"coverage report is incomplete for {source_id}")
        operation_count = summary.get("operations")
        callable_count = summary.get("callable_operations")
        if type(operation_count) is not int or type(callable_count) is not int or not 0 <= callable_count <= operation_count:
            raise AssertionError(f"coverage report counts are invalid for {source_id}")

        scope_kind = report_scope.get("kind")
        if source_id == "data_go_kr":
            if scope_kind != "aggregate_supported_catalog" or operations:
                raise AssertionError("data_go_kr denominator must remain aggregate and unenumerated")
        else:
            if scope_kind != "enumerated_supported_operations":
                raise AssertionError(f"{source_id} denominator must enumerate supported operations")
            identities = [row.get("operation_id") for row in operations if isinstance(row, dict)]
            if len(identities) != len(operations) or any(not isinstance(value, str) or not value for value in identities):
                raise AssertionError(f"enumerated operation identities are invalid for {source_id}")
            if len(identities) != len(set(identities)) or len(identities) != operation_count:
                raise AssertionError(f"enumerated operation identities do not match the denominator for {source_id}")
            if sum(row.get("callable") is True for row in operations) != callable_count:
                raise AssertionError(f"callable operation identities do not match the denominator for {source_id}")

        contribution = rollup_by_id[source_id]
        expected_contribution = {
            "source_id": source_id,
            "path": coverage_path,
            "scope_kind": scope_kind,
            "operations": operation_count,
            "callable_operations": callable_count,
        }
        if contribution != expected_contribution:
            raise AssertionError(f"denominator rollup contribution does not match policy-bound report for {source_id}")
        operation_total += operation_count
        callable_total += callable_count

    rollup_summary = rollup.get("summary")
    if not isinstance(rollup_summary, dict) or rollup_summary != {
        "sources": len(EXPECTED_POLICY_SOURCE_IDS),
        "operations": operation_total,
        "callable_operations": callable_total,
    }:
        raise AssertionError("denominator rollup summary does not match policy-bound source reports")
    return operation_total


class RuntimeFreshnessQueueTest(unittest.TestCase):
    def test_latest_timestamp_wins_over_unknown_and_older_result(self) -> None:
        latest = MODULE.latest_by_identity([
            {"identity_key": "data_go_kr:d:o", "status": "verified"},
            {"identity_key": "data_go_kr:d:o", "status": "failed", "verified_at": "2026-06-01T00:00:00Z"},
            {"identity_key": "data_go_kr:d:o", "status": "verified", "verified_at": "2026-07-01T00:00:00Z"},
        ])
        self.assertEqual(latest["data_go_kr:d:o"]["status"], "verified")

    def test_missing_timestamp_never_counts_as_fresh(self) -> None:
        result = MODULE.classify({"status": "verified"}, datetime(2026, 7, 4, tzinfo=timezone.utc), 30, 90)
        self.assertEqual(result, ("unknown_timestamp", 1, "repair_evidence_timestamp"))

    def test_latest_verification_time_allows_evidence_newer_than_release(self) -> None:
        original_load = MODULE.load
        denominator = {
            "generated_at": "2026-07-11T00:00:00Z",
            "summary": {"operations": 1},
            "sources": [],
        }
        latest = {
            "generated_at": "2026-07-24T00:00:01Z",
            "results": [{
                "dataset_id": "d",
                "operation": "o",
                "status": "verified",
                "verified_at": "2026-07-24T00:00:00Z",
            }],
        }
        projection = {
            "freshness": {"as_of": latest["generated_at"]},
            "current_evidence": [{"identity_key": "data_go_kr:d:1", "status": "verified", "verified_at": "2026-07-24T00:00:00Z"}],
            "records": [],
            "operations": [{"identity_key": "data_go_kr:d:1", "contract_complete": True, "incomplete_reasons": []}],
            "summary": {},
        }
        policy = {
            "freshness": {
                "fresh_days": 30,
                "expire_days": 90,
                "evaluation_time_source": "latest_verification.generated_at",
            },
        }

        def load_fixture(path: pathlib.Path):
            values = {
                MODULE.DENOMINATORS: denominator,
                MODULE.REGISTRY: [],
                MODULE.LATEST: latest,
                MODULE.PROJECTION: projection,
                MODULE.POLICY: policy,
            }
            return values[path] if path in values else original_load(path)

        operation = {
            "source_id": "data_go_kr",
            "dataset_id": "d",
            "operation": "o",
            "operation_seq": "1",
            "identity_key": "data_go_kr:d:1",
        }
        with (
            mock.patch.object(MODULE, "load", side_effect=load_fixture),
            mock.patch.object(MODULE, "supported_operations", return_value=[operation]),
        ):
            report = MODULE.build()

        self.assertEqual(report["generated_at"], latest["generated_at"])
        self.assertEqual(report["freshness"]["as_of"], latest["generated_at"])
        self.assertEqual(report["summary"]["fresh_verified"], 1)

    def test_non_data_go_denominator_is_explicitly_outside_projection_scope(self) -> None:
        original_load = MODULE.load
        latest = {"generated_at": "2026-07-24T00:00:01Z", "results": []}
        projection = {"freshness": {"as_of": latest["generated_at"]}, "current_evidence": [], "records": [], "summary": {}}
        denominator = {
            "summary": {"operations": 1},
            "sources": [{"source_id": "ecos", "path": "reports/ecos/operation-denominator.json"}],
        }
        source_denominator = {"operations": [{"operation_id": "ecos-statistic-search-102y004"}]}
        policy = {"freshness": {"fresh_days": 30, "expire_days": 90, "evaluation_time_source": "latest_verification.generated_at"}}

        def load_fixture(path: pathlib.Path):
            values = {
                MODULE.DENOMINATORS: denominator,
                pathlib.Path("reports/ecos/operation-denominator.json"): source_denominator,
                MODULE.REGISTRY: [],
                MODULE.LATEST: latest,
                MODULE.PROJECTION: projection,
                MODULE.POLICY: policy,
            }
            return values[path] if path in values else original_load(path)

        with mock.patch.object(MODULE, "load", side_effect=load_fixture):
            report = MODULE.build()

        self.assertEqual(report["summary"]["unsupported_current_binding"], 1)
        self.assertEqual(report["summary"]["fresh_verified"], 0)
        self.assertEqual(report["queue"][0]["classification"], "unsupported_current_binding")
        self.assertEqual(report["queue"][0]["action"], "review_source_specific_receipt")
        self.assertIn("source-specific receipt contract", report["queue"][0]["reason"])

    def test_incomplete_data_go_contract_is_explicit_and_not_sent_to_runtime_batch(self) -> None:
        original_load = MODULE.load
        latest = {"generated_at": "2026-07-24T00:00:01Z", "results": []}
        projection = {
            "freshness": {"as_of": latest["generated_at"]},
            "current_evidence": [],
            "records": [],
            "operations": [
                {"identity_key": "data_go_kr:d:complete", "contract_complete": True, "incomplete_reasons": []},
                {"identity_key": "data_go_kr:d:incomplete", "contract_complete": False, "incomplete_reasons": ["missing_exact_endpoint"]},
            ],
            "summary": {},
        }
        denominator = {"summary": {"operations": 2}, "sources": []}
        policy = {"freshness": {"fresh_days": 30, "expire_days": 90, "evaluation_time_source": "latest_verification.generated_at"}}
        operations = [
            {"source_id": "data_go_kr", "dataset_id": "d", "operation": "complete", "operation_seq": None, "identity_key": "data_go_kr:d:complete"},
            {"source_id": "data_go_kr", "dataset_id": "d", "operation": "incomplete", "operation_seq": None, "identity_key": "data_go_kr:d:incomplete"},
        ]

        def load_fixture(path: pathlib.Path):
            values = {MODULE.DENOMINATORS: denominator, MODULE.REGISTRY: [], MODULE.LATEST: latest, MODULE.PROJECTION: projection, MODULE.POLICY: policy}
            return values[path] if path in values else original_load(path)

        with mock.patch.object(MODULE, "load", side_effect=load_fixture), mock.patch.object(MODULE, "supported_operations", return_value=operations):
            report = MODULE.build()

        self.assertEqual(report["summary"]["unsupported_current_binding"], 1)
        unsupported = next(row for row in report["queue"] if row["identity_key"] == "data_go_kr:d:incomplete")
        self.assertEqual(unsupported["classification"], "unsupported_current_binding")
        self.assertIn("missing_exact_endpoint", unsupported["reason"])

    def test_current_queue_reconciles_complete_denominator(self) -> None:
        registry_bytes = MODULE.REGISTRY.read_bytes() if MODULE.REGISTRY.is_file() else b""
        if registry_bytes.startswith(b"version https://git-lfs.github.com/spec/v1") or not MODULE.PROJECTION.is_file():
            self.skipTest("full registry source or regenerated current evidence projection is unavailable")
        policy = json.loads((ROOT / "policy" / "sustainable-coverage.json").read_text(encoding="utf-8"))
        source_reports = {
            source["source_id"]: json.loads((ROOT / source["coverage_report"]).read_text(encoding="utf-8"))
            for source in policy["supported_sources"]
        }
        rollup = json.loads(MODULE.DENOMINATORS.read_text(encoding="utf-8"))
        expected_total = policy_bound_operation_total(policy, source_reports, rollup)
        report = MODULE.build()
        summary = report["summary"]
        self.assertEqual(summary["supported_operations"], expected_total)
        self.assertEqual(summary["queued"] + summary["fresh_verified"], expected_total)
        self.assertEqual(len(report["queue"]), summary["queued"])

    def test_policy_bound_growth_is_accepted_and_mismatches_are_rejected(self) -> None:
        policy = json.loads((ROOT / "policy" / "sustainable-coverage.json").read_text(encoding="utf-8"))
        source_reports = {
            source["source_id"]: json.loads((ROOT / source["coverage_report"]).read_text(encoding="utf-8"))
            for source in policy["supported_sources"]
        }
        rollup = json.loads(MODULE.DENOMINATORS.read_text(encoding="utf-8"))
        baseline = policy_bound_operation_total(policy, source_reports, rollup)

        grown_reports = copy.deepcopy(source_reports)
        grown_rollup = copy.deepcopy(rollup)
        # data.go.kr is intentionally aggregate-only, so a catalogue addition
        # changes its source summary without inventing an operation identity.
        grown_reports["data_go_kr"]["summary"]["operations"] += 1
        grown_reports["data_go_kr"]["summary"]["callable_operations"] += 1
        grown_row = next(row for row in grown_rollup["sources"] if row["source_id"] == "data_go_kr")
        grown_row["operations"] += 1
        grown_row["callable_operations"] += 1
        grown_rollup["summary"]["operations"] += 1
        grown_rollup["summary"]["callable_operations"] += 1
        self.assertEqual(policy_bound_operation_total(policy, grown_reports, grown_rollup), baseline + 1)

        wrong_identity_reports = copy.deepcopy(source_reports)
        wrong_identity_reports["ecos"]["source_id"] = "kosis"
        with self.assertRaisesRegex(AssertionError, "identity does not match policy"):
            policy_bound_operation_total(policy, wrong_identity_reports, rollup)

        wrong_contribution = copy.deepcopy(rollup)
        next(row for row in wrong_contribution["sources"] if row["source_id"] == "data_go_kr")["operations"] += 1
        with self.assertRaisesRegex(AssertionError, "contribution does not match"):
            policy_bound_operation_total(policy, source_reports, wrong_contribution)

        duplicate_policy_source = copy.deepcopy(policy)
        duplicate_policy_source["supported_sources"].append(copy.deepcopy(duplicate_policy_source["supported_sources"][0]))
        with self.assertRaisesRegex(AssertionError, "exactly once"):
            policy_bound_operation_total(duplicate_policy_source, source_reports, rollup)

    def test_queue_builder_tracks_a_supported_catalogue_growth(self) -> None:
        # Exercise the actual queue builder with a tiny fixture: one additional
        # data.go.kr operation must increase both the built queue and the
        # independently reconciled policy denominator by one.
        source_ids = sorted(EXPECTED_POLICY_SOURCE_IDS)
        configured_sources = []
        source_reports = {}
        rollup_rows = []
        report_paths = {}
        for source_id in source_ids:
            aggregate = source_id == "data_go_kr"
            path = pathlib.Path("fixture") / source_id / "operation-denominator.json"
            operations = [] if aggregate else [{"operation_id": f"{source_id}-operation", "callable": True}]
            count = 2 if aggregate else 1
            report = {
                "schema_version": "datapan.operation-denominator.v1",
                "source_id": source_id,
                "scope": {"kind": "aggregate_supported_catalog" if aggregate else "enumerated_supported_operations"},
                "summary": {"operations": count, "callable_operations": count},
                "operations": operations,
            }
            configured_sources.append({"source_id": source_id, "catalog_scope": "operation_denominator", "coverage_report": path.as_posix()})
            source_reports[source_id] = report
            report_paths[path] = report
            rollup_rows.append({
                "source_id": source_id,
                "path": path.as_posix(),
                "scope_kind": report["scope"]["kind"],
                "operations": count,
                "callable_operations": count,
            })

        policy = {
            "supported_sources": configured_sources,
            "freshness": {"fresh_days": 30, "expire_days": 90, "evaluation_time_source": "latest_verification.generated_at"},
        }
        rollup = {
            "summary": {"sources": len(source_ids), "operations": 6, "callable_operations": 6},
            "sources": rollup_rows,
        }
        latest = {"generated_at": "2026-09-01T00:00:00Z", "results": []}
        projection = {"freshness": {"as_of": latest["generated_at"]}, "current_evidence": [], "records": [], "operations": [], "summary": {}}
        registry = [{"id": "fixture-dataset", "operations": [{"name": "first"}, {"name": "second"}]}]
        values = {
            MODULE.DENOMINATORS: rollup,
            MODULE.REGISTRY: registry,
            MODULE.LATEST: latest,
            MODULE.PROJECTION: projection,
            MODULE.POLICY: policy,
            **report_paths,
        }
        original_load = MODULE.load

        def load_fixture(path: pathlib.Path):
            return values[path] if path in values else original_load(path)

        baseline_total = policy_bound_operation_total(policy, source_reports, rollup)
        with mock.patch.object(MODULE, "load", side_effect=load_fixture):
            baseline_report = MODULE.build()

            grown_registry = copy.deepcopy(registry)
            grown_registry[0]["operations"].append({"name": "third"})
            grown_reports = copy.deepcopy(source_reports)
            grown_reports["data_go_kr"]["summary"]["operations"] += 1
            grown_reports["data_go_kr"]["summary"]["callable_operations"] += 1
            grown_rollup = copy.deepcopy(rollup)
            next(row for row in grown_rollup["sources"] if row["source_id"] == "data_go_kr")["operations"] += 1
            next(row for row in grown_rollup["sources"] if row["source_id"] == "data_go_kr")["callable_operations"] += 1
            grown_rollup["summary"]["operations"] += 1
            grown_rollup["summary"]["callable_operations"] += 1
            grown_values = dict(values)
            grown_values[MODULE.REGISTRY] = grown_registry
            grown_values[MODULE.DENOMINATORS] = grown_rollup
            grown_values.update({path: grown_reports[source_id] for source_id, source in zip(source_ids, configured_sources) for path in [pathlib.Path(source["coverage_report"])]})
            values.clear()
            values.update(grown_values)
            grown_total = policy_bound_operation_total(policy, grown_reports, grown_rollup)
            grown_report = MODULE.build()

        self.assertEqual(baseline_total, 6)
        self.assertEqual(baseline_report["summary"]["supported_operations"], baseline_total)
        self.assertEqual(baseline_report["summary"]["fresh_verified"], 0)
        self.assertEqual(grown_total, baseline_total + 1)
        self.assertEqual(grown_report["summary"]["supported_operations"], grown_total)
        self.assertEqual(grown_report["summary"]["fresh_verified"], 0)
        self.assertEqual(grown_report["summary"]["queued"], baseline_report["summary"]["queued"] + 1)
        self.assertEqual(grown_report["summary"]["queued"] + grown_report["summary"]["fresh_verified"], grown_total)
        self.assertEqual(len(grown_report["queue"]), grown_report["summary"]["queued"])


if __name__ == "__main__":
    unittest.main()
