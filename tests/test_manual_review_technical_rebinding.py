from __future__ import annotations

import datetime as dt
import hashlib
import importlib.util
import copy
import json
import pathlib
import sys
import tempfile
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import manual_review_scope

SPEC = importlib.util.spec_from_file_location(
    "technical_rebinding", ROOT / "scripts" / "generate-credential-runtime-manual-review-technical-rebinding.py"
)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(MODULE)
ACCEPTANCE_SPEC = importlib.util.spec_from_file_location(
    "technical_rebinding_acceptance",
    ROOT / "scripts" / "generate-credential-runtime-manual-review-acceptance.py",
)
ACCEPTANCE = importlib.util.module_from_spec(ACCEPTANCE_SPEC)
assert ACCEPTANCE_SPEC.loader is not None
ACCEPTANCE_SPEC.loader.exec_module(ACCEPTANCE)


class ManualReviewTechnicalRebindingTest(unittest.TestCase):
    def policy(self, decision_path: pathlib.Path, baseline: list[dict]) -> dict:
        return {
            "generated_at": "2026-07-23T00:00:00Z",
            "approver_scope": "approved artifact-only technical rebinding",
            "decision_path": decision_path.as_posix(),
            "decision_sha256": MODULE.digest_bytes(decision_path.read_bytes()),
            "baseline": {"artifact_count": len(baseline), "artifact_contract_sha256": MODULE.artifact_contract_digest(baseline)},
            "allowed_additions": [
                {"path": "schemas/health.schema.json", "kind": "schema"},
                {"path": "reports/health.json", "kind": "verification_plan", "schema": "https://schemas.example/health"},
            ],
            "independent_additions": [],
        }

    def explicit_decision(self, expires_at: str):
        def load(path: str) -> dict:
            return json.loads((ROOT / path).read_text(encoding="utf-8"))

        decision = load("reports/credential-runtime-manual-review-decision.json")
        compatibility = load("reports/release-consumer-compatibility.json")
        handoff = load("reports/credential-runtime-review-handoff.json")
        manifest = load("manifest.json")
        health_plan = load("reports/health-runtime-observation-plan.v1.json")
        health_selection = load("policy/health-runtime-observation-selection.json")
        handoff_path = ROOT / "reports/credential-runtime-review-handoff.json"
        compatibility_path = ROOT / "reports/release-consumer-compatibility.json"
        handoff_sha = hashlib.sha256(handoff_path.read_bytes()).hexdigest()
        body = decision["decision"]
        body.update(
            reviewer="Accountable reviewer",
            reviewed_at="2026-09-01T00:00:00Z",
            reason="Revalidated the unchanged manual-review scope.",
            expires_at=expires_at,
            handoff_sha256=handoff_sha,
            review_scope_version=manual_review_scope.REVIEW_SCOPE_VERSION,
        )
        body["review_scope_sha256"] = manual_review_scope.review_scope_sha256(
            compatibility=compatibility,
            handoff_sha256=handoff_sha,
            manifest=manifest,
            health_plan=health_plan,
            health_selection=health_selection,
        )
        decision_bytes = (json.dumps(decision, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
        evaluation = manual_review_scope.evaluate_review_scope(
            decision=decision,
            decision_sha256=hashlib.sha256(decision_bytes).hexdigest(),
            compatibility=compatibility,
            handoff=handoff,
            handoff_sha256=handoff_sha,
            manifest=manifest,
            health_plan=health_plan,
            health_selection=health_selection,
            as_of=dt.datetime(2026, 10, 1, tzinfo=dt.timezone.utc),
        )
        return decision, decision_bytes, {
            "evaluation": evaluation,
            "compatibility": compatibility,
            "handoff": handoff,
            "manifest": manifest,
            "health_plan": health_plan,
            "health_selection": health_selection,
            "handoff_path": handoff_path,
            "compatibility_path": compatibility_path,
        }

    def test_only_the_exact_two_health_artifacts_can_rebind_after_canonical_regeneration(self):
        with tempfile.TemporaryDirectory() as raw:
            root = pathlib.Path(raw)
            decision_path = root / "decision.json"
            decision_path.write_text(json.dumps({"decision": {"compatibility_sha256": "a" * 64}}), encoding="utf-8")
            baseline = [{"path": "data/registry.json", "kind": "registry", "bytes": 1, "sha256": "b" * 64}]
            regenerated_baseline = [{"path": "data/registry.json", "kind": "registry", "bytes": 99, "sha256": "e" * 64}]
            artifacts = regenerated_baseline + [
                {"path": "schemas/health.schema.json", "kind": "schema", "bytes": 2, "sha256": "c" * 64},
                {"path": "reports/health.json", "kind": "verification_plan", "schema": "https://schemas.example/health", "bytes": 3, "sha256": "d" * 64},
            ]
            value = MODULE.expected(self.policy(decision_path, baseline), {"artifacts": artifacts}, {"summary": {}}, decision_path)
            self.assertEqual(value["status"], "approved_artifact_only_rebinding")
            self.assertEqual(value["old_compatibility_sha256"], "a" * 64)
            self.assertTrue(value["historical_rebinding_eligible"])
            self.assertEqual(value["manifest_delta"]["added_paths"], ["reports/health.json", "schemas/health.schema.json"])
            with self.assertRaisesRegex(ValueError, "allowlist"):
                MODULE.expected(self.policy(decision_path, baseline), {"artifacts": artifacts + [{"path": "reports/extra.json", "kind": "extra"}]}, {"summary": {}}, decision_path)

    def test_exact_independent_ticket_artifacts_do_not_expand_health_approval(self):
        with tempfile.TemporaryDirectory() as raw:
            root = pathlib.Path(raw)
            decision_path = root / "decision.json"
            decision_path.write_text(json.dumps({"decision": {"compatibility_sha256": "a" * 64}}), encoding="utf-8")
            baseline = [{"path": "data/registry.json", "kind": "registry"}]
            policy = self.policy(decision_path, baseline)
            policy["independent_additions"] = [
                {
                    "path": "schemas/completeness.schema.json",
                    "kind": "schema",
                    "authority_ticket": "StatPan/datapan-registry#631",
                }
            ]
            artifacts = baseline + policy["allowed_additions"] + [
                {"path": "schemas/completeness.schema.json", "kind": "schema"}
            ]

            value = MODULE.expected(policy, {"artifacts": artifacts}, {"summary": {}}, decision_path)

            self.assertEqual(
                value["manifest_delta"]["independent_paths"],
                ["schemas/completeness.schema.json"],
            )
            self.assertEqual(
                value["independent_additions"][0]["authority_ticket"],
                "StatPan/datapan-registry#631",
            )
            with self.assertRaisesRegex(ValueError, "allowlist"):
                MODULE.expected(
                    policy,
                    {"artifacts": artifacts + [{"path": "reports/unowned.json", "kind": "coverage"}]},
                    {"summary": {}},
                    decision_path,
                )

    def test_production_independent_authorities_are_an_exact_allowlist(self):
        policy = json.loads((ROOT / "policy/health-observation-plan-technical-rebinding.json").read_text())
        MODULE.validate_independent_authorities(policy["independent_additions"])

        for mutate in (
            lambda rows: rows[-1].update(authority_ticket="StatPan/datapan-registry#631"),
            lambda rows: rows[-1].update(path="schemas/other-review-scope.schema.json"),
            lambda rows: rows.append({
                "path": "schemas/arbitrary.schema.json",
                "kind": "schema",
                "authority_ticket": "StatPan/datapan-registry#661",
            }),
        ):
            altered = copy.deepcopy(policy["independent_additions"])
            mutate(altered)
            with self.subTest(altered=altered[-1]), self.assertRaisesRegex(ValueError, "exact separately authorized"):
                MODULE.validate_independent_authorities(altered)

    def test_preexisting_artifact_contract_change_rejects_rebinding(self):
        with tempfile.TemporaryDirectory() as raw:
            root = pathlib.Path(raw)
            decision_path = root / "decision.json"
            decision_path.write_text(json.dumps({"decision": {"compatibility_sha256": "a" * 64}}), encoding="utf-8")
            baseline = [{"path": "data/registry.json", "kind": "registry", "bytes": 1, "sha256": "b" * 64}]
            artifacts = [{"path": "data/registry.json", "kind": "different_kind", "bytes": 1, "sha256": "b" * 64}] + [
                {"path": "schemas/health.schema.json", "kind": "schema"},
                {"path": "reports/health.json", "kind": "verification_plan", "schema": "https://schemas.example/health"},
            ]
            with self.assertRaisesRegex(ValueError, "allowlist"):
                MODULE.expected(self.policy(decision_path, baseline), {"artifacts": artifacts}, {"summary": {}}, decision_path)

    def test_tampering_the_human_decision_rejects_rebinding(self):
        with tempfile.TemporaryDirectory() as raw:
            root = pathlib.Path(raw)
            decision_path = root / "decision.json"
            decision_path.write_text(json.dumps({"decision": {"compatibility_sha256": "a" * 64}}), encoding="utf-8")
            baseline = [{"path": "data/registry.json", "kind": "registry", "bytes": 1, "sha256": "b" * 64}]
            policy = self.policy(decision_path, baseline)
            decision_path.write_text(json.dumps({"decision": {"compatibility_sha256": "e" * 64}}), encoding="utf-8")
            artifacts = baseline + [
                {"path": "schemas/health.schema.json", "kind": "schema"},
                {"path": "reports/health.json", "kind": "verification_plan", "schema": "https://schemas.example/health"},
            ]
            with self.assertRaisesRegex(ValueError, "byte-for-byte unchanged"):
                MODULE.expected(policy, {"artifacts": artifacts}, {"summary": {}}, decision_path)

    def test_explicit_future_review_is_current_scope_only_not_historical_rebinding(self):
        decision, decision_bytes, context = self.explicit_decision("2026-12-31T00:00:00Z")
        with tempfile.TemporaryDirectory() as raw:
            decision_path = pathlib.Path(raw) / "decision.json"
            decision_path.write_bytes(decision_bytes)
            policy = json.loads((ROOT / "policy/health-observation-plan-technical-rebinding.json").read_text())
            rebinding = MODULE.expected(
                policy,
                context["manifest"],
                context["compatibility"],
                decision_path,
                scope_evaluation=context["evaluation"],
            )
            acceptance = ACCEPTANCE.build_report(
                context["handoff"],
                context["compatibility"],
                decision,
                handoff_path=context["handoff_path"],
                compatibility_path=context["compatibility_path"],
                decision_path=decision_path,
                manifest=context["manifest"],
                health_plan=context["health_plan"],
                health_selection=context["health_selection"],
                as_of=dt.datetime(2026, 10, 1, tzinfo=dt.timezone.utc),
            )

        current_sha = hashlib.sha256(decision_bytes).hexdigest()
        self.assertNotEqual(current_sha, policy["decision_sha256"])
        self.assertEqual(rebinding["status"], "outside_historical_rebinding_scope")
        self.assertFalse(rebinding["historical_rebinding_eligible"])
        self.assertEqual(rebinding["revalidation_reason"], "manifest_delta_outside_approved_allowlist")
        self.assertFalse(rebinding["manifest_delta"]["scope_matches_approved_allowlist"])
        self.assertEqual(rebinding["decision_sha256"], current_sha)
        self.assertEqual(rebinding["historical_decision_sha256"], policy["decision_sha256"])
        self.assertEqual(
            rebinding["old_compatibility_sha256"],
            context["evaluation"]["reviewed_binding"]["historical_decision_compatibility_sha256"],
        )
        self.assertTrue(acceptance["summary"]["accepted"])
        self.assertEqual(acceptance["summary"]["acceptance_status"], "accepted")
        self.assertEqual(acceptance["review_scope"]["current_binding"]["decision_sha256"], current_sha)

    def test_expired_future_review_derives_pending_acceptance_and_open_goal(self):
        decision, decision_bytes, context = self.explicit_decision("2026-09-30T00:00:00Z")
        with tempfile.TemporaryDirectory() as raw:
            decision_path = pathlib.Path(raw) / "decision.json"
            decision_path.write_bytes(decision_bytes)
            policy = json.loads((ROOT / "policy/health-observation-plan-technical-rebinding.json").read_text())
            rebinding = MODULE.expected(
                policy,
                context["manifest"],
                context["compatibility"],
                decision_path,
                scope_evaluation=context["evaluation"],
            )
            acceptance = ACCEPTANCE.build_report(
                context["handoff"],
                context["compatibility"],
                decision,
                handoff_path=context["handoff_path"],
                compatibility_path=context["compatibility_path"],
                decision_path=decision_path,
                manifest=context["manifest"],
                health_plan=context["health_plan"],
                health_selection=context["health_selection"],
                as_of=dt.datetime(2026, 10, 1, tzinfo=dt.timezone.utc),
            )

        self.assertEqual(rebinding["status"], "outside_historical_rebinding_scope")
        self.assertFalse(rebinding["historical_rebinding_eligible"])
        self.assertEqual(rebinding["revalidation_reason"], "manifest_delta_outside_approved_allowlist")
        self.assertFalse(rebinding["manifest_delta"]["scope_matches_approved_allowlist"])
        self.assertGreater(
            rebinding["manifest_delta"]["stripped_artifact_count"],
            rebinding["baseline"]["artifact_count"],
        )
        self.assertEqual(
            rebinding["manifest_delta"]["stripped_artifact_contract_sha256"],
            MODULE.artifact_contract_digest(
                [
                    item
                    for item in context["manifest"]["artifacts"]
                    if item["path"] not in {
                        row["path"]
                        for row in policy["allowed_additions"] + policy["independent_additions"]
                    }
                ]
            ),
        )
        self.assertEqual(rebinding["historical_decision_sha256"], policy["decision_sha256"])
        self.assertFalse(acceptance["summary"]["accepted"])
        self.assertEqual(acceptance["summary"]["acceptance_status"], "revalidation_required")
        self.assertEqual(
            acceptance["release_boundary"]["goal_completion_effect"],
            "goal_remains_open_until_reviewed_receipts_or_explicit_acceptance",
        )
        self.assertTrue(acceptance["review_scope"]["decision_expired"])

    def test_invalid_historical_binding_does_not_soften_manifest_boundary(self):
        decision, decision_bytes, context = self.explicit_decision("2026-09-30T00:00:00Z")
        with tempfile.TemporaryDirectory() as raw:
            decision_path = pathlib.Path(raw) / "decision.json"
            decision_path.write_bytes(decision_bytes)
            policy = json.loads((ROOT / "policy/health-observation-plan-technical-rebinding.json").read_text())
            scope = copy.deepcopy(context["evaluation"])
            scope["historical_decision_valid"] = False
            with self.assertRaisesRegex(ValueError, "historical manual-review proof"):
                MODULE.expected(
                    policy,
                    context["manifest"],
                    context["compatibility"],
                    decision_path,
                    scope_evaluation=scope,
                )

    def test_unproven_scope_does_not_soften_manifest_boundary(self):
        decision, decision_bytes, context = self.explicit_decision("2026-09-30T00:00:00Z")
        with tempfile.TemporaryDirectory() as raw:
            decision_path = pathlib.Path(raw) / "decision.json"
            decision_path.write_bytes(decision_bytes)
            policy = json.loads((ROOT / "policy/health-observation-plan-technical-rebinding.json").read_text())
            scope = copy.deepcopy(context["evaluation"])
            scope["scope_status"] = "unproven"
            with self.assertRaisesRegex(ValueError, "allowlist"):
                MODULE.expected(
                    policy,
                    context["manifest"],
                    context["compatibility"],
                    decision_path,
                    scope_evaluation=scope,
                )

    def test_duplicate_artifact_still_fails_before_scope_revalidation_receipt(self):
        decision, decision_bytes, context = self.explicit_decision("2026-09-30T00:00:00Z")
        with tempfile.TemporaryDirectory() as raw:
            decision_path = pathlib.Path(raw) / "decision.json"
            decision_path.write_bytes(decision_bytes)
            policy = json.loads((ROOT / "policy/health-observation-plan-technical-rebinding.json").read_text())
            manifest = copy.deepcopy(context["manifest"])
            manifest["artifacts"].append(copy.deepcopy(manifest["artifacts"][0]))
            with self.assertRaisesRegex(ValueError, "duplicate path"):
                MODULE.expected(
                    policy,
                    manifest,
                    context["compatibility"],
                    decision_path,
                    scope_evaluation=context["evaluation"],
                )
