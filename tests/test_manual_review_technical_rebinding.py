from __future__ import annotations

import datetime as dt
import hashlib
import importlib.util
import copy
import json
import pathlib
import subprocess
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

    def queue_manifest_additions(self) -> list[dict[str, object]]:
        additions = []
        for authority in MODULE.EXPECTED_INDEPENDENT_ADDITIONS[-4:]:
            artifact = {key: value for key, value in authority.items() if key != "authority_ticket"}
            artifact_bytes = (ROOT / artifact["path"]).read_bytes()
            artifact["bytes"] = len(artifact_bytes)
            artifact["sha256"] = hashlib.sha256(artifact_bytes).hexdigest()
            additions.append(artifact)
        return additions

    def historical_manifest_with_authorized_additions(self) -> dict:
        """Build the historical approved contract from its exact committed 161-row fixture."""
        historical_commit = "2cb6ca7a66704afee99d037ed7a196d0bb064297"
        historical_path = "tests/fixtures/manual-review-scope/manifest.json"
        historical_blob = "922cae69d7534f8cf20e9bc1090e2f4a2db9f325"
        historical_sha256 = "c42f9f8fa56e31459524ac5684e8b0de05c59102822d0f1289ffe7235eca2217"
        raw = subprocess.check_output(
            ["git", "show", f"{historical_commit}:{historical_path}"], cwd=ROOT,
        )
        self.assertEqual(
            subprocess.check_output(
                ["git", "rev-parse", f"{historical_commit}:{historical_path}"],
                cwd=ROOT, text=True,
            ).strip(),
            historical_blob,
        )
        self.assertEqual(hashlib.sha256(raw).hexdigest(), historical_sha256)
        historical = json.loads(raw)
        policy = json.loads((ROOT / "policy/health-observation-plan-technical-rebinding.json").read_bytes())
        self.assertEqual(len(historical["artifacts"]), 161)
        self.assertEqual(policy["baseline"]["artifact_count"], 161)
        self.assertEqual(
            MODULE.artifact_contract_digest(historical["artifacts"]),
            policy["baseline"]["artifact_contract_sha256"],
        )

        current = json.loads((ROOT / "manifest.json").read_bytes())
        current_rows: dict[str, dict[str, object]] = {}
        for row in current["artifacts"]:
            path = row.get("path")
            if isinstance(path, str):
                self.assertNotIn(path, current_rows, f"current manifest repeats {path}")
                current_rows[path] = row
        queue_rows = {row["path"]: row for row in self.queue_manifest_additions()}
        historical_paths = {row["path"] for row in historical["artifacts"]}
        additions: list[dict[str, object]] = []
        for authority in policy["allowed_additions"] + policy["independent_additions"]:
            path = authority["path"]
            self.assertNotIn(path, historical_paths, f"authorized addition is already in baseline: {path}")
            row = copy.deepcopy(queue_rows.get(path) or current_rows.get(path))
            self.assertIsNotNone(row, f"authorized artifact row is unavailable: {path}")
            assert row is not None
            for key, value in authority.items():
                if key != "authority_ticket":
                    self.assertEqual(row.get(key), value, f"authorized artifact identity changed: {path}.{key}")
            additions.append(row)

        result = copy.deepcopy(historical)
        result["artifacts"].extend(additions)
        result["artifact_count"] = len(result["artifacts"])
        self.assertEqual(result["artifact_count"], 205)
        stripped = [
            row for row in result["artifacts"]
            if row.get("path") not in {
                item["path"] for item in policy["allowed_additions"] + policy["independent_additions"]
            }
        ]
        self.assertEqual(
            MODULE.artifact_contract_digest(stripped),
            policy["baseline"]["artifact_contract_sha256"],
        )
        return result

    def historical_scope_fixture_payloads(self) -> dict[str, bytes]:
        """Read every historical scope input from its pinned commit and verify its identity."""
        historical_commit = "2cb6ca7a66704afee99d037ed7a196d0bb064297"
        expected_oids = {
            "decision.json": "0c0b72dc647c49e36617ced810f1d1a48c6f076b",
            "compatibility.json": "20646338e3adefdf96cfc457c3aecba85d8b2399",
            "handoff.json": "6e74c555d96cea7f7c8ae72d080e51b0e3d64a9d",
            "manifest.json": "922cae69d7534f8cf20e9bc1090e2f4a2db9f325",
            "health-plan.json": "c67be97bac04664cae57f9c1aaa6280b3062c230",
            "health-selection.json": "c9eb346fbe7c6dbdffd140ed4d1caf4dfbe5e7af",
        }
        payloads: dict[str, bytes] = {}
        for name, expected_oid in expected_oids.items():
            path = f"tests/fixtures/manual-review-scope/{name}"
            raw = subprocess.check_output(["git", "show", f"{historical_commit}:{path}"], cwd=ROOT)
            actual_oid = subprocess.check_output(
                ["git", "rev-parse", f"{historical_commit}:{path}"], cwd=ROOT, text=True,
            ).strip()
            self.assertEqual(actual_oid, expected_oid, name)
            self.assertEqual(hashlib.sha256(raw).hexdigest(), manual_review_scope.FIXTURE_SHA256[name], name)
            payloads[name] = raw
        return payloads

    def explicit_decision(
        self,
        expires_at: str,
        *,
        manifest_override: dict | None = None,
        add_queue_additions: bool = True,
    ):
        def load(path: str) -> dict:
            return json.loads((ROOT / path).read_text(encoding="utf-8"))

        decision = load("reports/credential-runtime-manual-review-decision.json")
        compatibility = load("reports/release-consumer-compatibility.json")
        handoff = load("reports/credential-runtime-review-handoff.json")
        manifest = copy.deepcopy(manifest_override) if manifest_override is not None else load("manifest.json")
        queue_additions = self.queue_manifest_additions()
        queue_by_path = {artifact["path"]: artifact for artifact in queue_additions}
        queue_paths = set(queue_by_path)
        for path, expected_artifact in queue_by_path.items():
            existing = [artifact for artifact in manifest["artifacts"] if artifact.get("path") == path]
            if len(existing) > 1:
                raise ValueError(f"fixture manifest has duplicate queue registration: {path}")
            if existing and existing[0] != expected_artifact:
                raise ValueError(f"fixture manifest has conflicting queue registration: {path}")
        # Normalize either a pre-GEN or post-GEN input before constructing this
        # test fixture, so rerunning after ordinary generation never duplicates rows.
        manifest["artifacts"] = [
            artifact for artifact in manifest["artifacts"]
            if artifact.get("path") not in queue_paths
        ]
        if add_queue_additions:
            manifest["artifacts"].extend(queue_additions)
        manifest["artifact_count"] = len(manifest["artifacts"])
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

        self.assertEqual(policy["baseline"], {
            "artifact_count": 161,
            "artifact_contract_sha256": "44d9e29a76fa88df87c032b0b641ba3bcf4e861a243311b185a8e4039f28634a",
        })
        self.assertEqual(policy["decision_sha256"], "4d0bee81a67d4b49a1ccb022ab6be7a28a76b9613cbd7c2211d5f22ce9d69151")
        self.assertEqual(len(policy["allowed_additions"]), 7)
        self.assertEqual(
            hashlib.sha256(json.dumps(
                policy["allowed_additions"], ensure_ascii=False, sort_keys=True, separators=(",", ":")
            ).encode()).hexdigest(),
            "363db3c251e04056c03ee1b396f6b174944ce58ff869913ca9b6e25c81be88a2",
        )
        self.assertEqual(
            policy["approver_scope"],
            "StatPan #391 accepted manual-review decision for Health plan/schema rebinding; Goal #604 and its reused #598 version gate authorize only the listed static Registry artifacts, with no credential, provider-call, or Health-runtime change",
        )
        self.assertFalse(any(row.get("authority_ticket") == "StatPan/datapan-registry#767" for row in policy["allowed_additions"]))
        queue_rows = policy["independent_additions"][-4:]
        self.assertEqual(queue_rows, [
            {"path": "schemas/datapan.canonical-stale-c-queue-recovery-receipt.v1.schema.json", "kind": "schema", "authority_ticket": "StatPan/datapan-registry#767"},
            {"path": "schemas/datapan.canonical-stale-c-queue-recovery.v1.schema.json", "kind": "schema", "authority_ticket": "StatPan/datapan-registry#767"},
            {"path": "schemas/datapan.canonical-stale-queue-recovery-policy.v1.schema.json", "kind": "schema", "authority_ticket": "StatPan/datapan-registry#767"},
            {"path": "policy/canonical-stale-queue-recovery.v1.json", "kind": "canonical_stale_queue_recovery_policy", "schema": "https://statpan.com/schemas/datapan.canonical-stale-queue-recovery-policy.v1.schema.json", "authority_ticket": "StatPan/datapan-registry#767"},
        ])

        def queue_policy_row(rows):
            return next(row for row in rows if row.get("path") == "policy/canonical-stale-queue-recovery.v1.json")

        mutations = [
            ("missing", lambda rows: rows.pop(rows.index(queue_policy_row(rows)))),
            ("wrong_ticket", lambda rows: queue_policy_row(rows).update(authority_ticket="StatPan/datapan-registry#631")),
            ("wrong_kind", lambda rows: queue_policy_row(rows).update(kind="coverage")),
            ("wrong_schema", lambda rows: queue_policy_row(rows).update(schema="https://schemas.datapan.dev/wrong.schema.json")),
            ("wrong_path", lambda rows: queue_policy_row(rows).update(path="policy/other-policy.json")),
            ("extra", lambda rows: rows.append({
                "path": "schemas/arbitrary.schema.json",
                "kind": "schema",
                "authority_ticket": "StatPan/datapan-registry#661",
            })),
        ]
        for label, mutate in mutations:
            altered = copy.deepcopy(policy["independent_additions"])
            mutate(altered)
            with self.subTest(case=label), self.assertRaisesRegex(ValueError, "exact separately authorized"):
                MODULE.validate_independent_authorities(altered)

    def test_explicit_current_review_keeps_historical_baseline_and_exact_queue_delta(self):
        policy = json.loads((ROOT / "policy/health-observation-plan-technical-rebinding.json").read_text())
        decision, decision_bytes, context = self.explicit_decision(
            "2026-12-31T00:00:00Z",
            manifest_override=self.historical_manifest_with_authorized_additions(),
        )
        with tempfile.TemporaryDirectory() as raw:
            decision_path = pathlib.Path(raw) / "decision.json"
            decision_path.write_bytes(decision_bytes)
            rebinding = MODULE.expected(
                policy, context["manifest"], context["compatibility"], decision_path,
                scope_evaluation=context["evaluation"],
            )

        self.assertEqual(rebinding["status"], "explicit_current_scope_review")
        self.assertTrue(rebinding["historical_rebinding_eligible"])
        self.assertEqual(rebinding["baseline"], policy["baseline"])
        self.assertEqual(rebinding["historical_decision_sha256"], policy["decision_sha256"])
        self.assertEqual(rebinding["manifest_delta"]["artifact_count_before"], policy["baseline"]["artifact_count"])
        self.assertEqual(rebinding["manifest_delta"]["artifact_count_after"], len(context["manifest"]["artifacts"]))
        self.assertEqual(rebinding["manifest_delta"]["independent_paths"], sorted(
            row["path"] for row in policy["independent_additions"]
        ))
        queue_manifest_row = next(
            row for row in context["manifest"]["artifacts"]
            if row.get("path") == "policy/canonical-stale-queue-recovery.v1.json"
        )
        self.assertNotIn("authority_ticket", queue_manifest_row)

        queue_paths = {row["path"] for row in MODULE.EXPECTED_INDEPENDENT_ADDITIONS[-4:]}
        missing = copy.deepcopy(context["manifest"])
        missing["artifacts"] = [row for row in missing["artifacts"] if row.get("path") not in queue_paths]
        missing["artifact_count"] = len(missing["artifacts"])
        with tempfile.TemporaryDirectory() as raw:
            decision_path = pathlib.Path(raw) / "decision.json"
            decision_path.write_bytes(decision_bytes)
            with self.assertRaisesRegex(ValueError, "every independently authorized addition"):
                MODULE.expected(policy, missing, context["compatibility"], decision_path,
                                scope_evaluation=context["evaluation"])

    def test_explicit_review_normalizes_pre_and_post_generation_manifests(self):
        source_manifest = self.historical_manifest_with_authorized_additions()
        additions = self.queue_manifest_additions()
        queue_paths = {artifact["path"] for artifact in additions}
        before_generation = copy.deepcopy(source_manifest)
        before_generation["artifacts"] = [
            artifact for artifact in before_generation["artifacts"]
            if artifact.get("path") not in queue_paths
        ]
        before_generation["artifact_count"] = len(before_generation["artifacts"])
        after_generation = copy.deepcopy(before_generation)
        after_generation["artifacts"].extend(additions)
        after_generation["artifact_count"] = len(after_generation["artifacts"])
        self.assertEqual(before_generation["artifact_count"], 201)
        self.assertEqual(after_generation["artifact_count"], 205)

        policy = json.loads((ROOT / "policy/health-observation-plan-technical-rebinding.json").read_text())
        for label, input_manifest in (
            ("before_generation", before_generation),
            ("after_generation", after_generation),
        ):
            with self.subTest(source_manifest=label):
                _, decision_bytes, context = self.explicit_decision(
                    "2026-12-31T00:00:00Z", manifest_override=input_manifest,
                )
                for path in queue_paths:
                    self.assertEqual(
                        sum(artifact.get("path") == path for artifact in context["manifest"]["artifacts"]),
                        1,
                    )
                with tempfile.TemporaryDirectory() as raw:
                    decision_path = pathlib.Path(raw) / "decision.json"
                    decision_path.write_bytes(decision_bytes)
                    rebinding = MODULE.expected(
                        policy, context["manifest"], context["compatibility"], decision_path,
                        scope_evaluation=context["evaluation"],
                    )
                self.assertEqual(rebinding["status"], "explicit_current_scope_review")
                self.assertTrue(rebinding["historical_rebinding_eligible"])

        _, absent_decision_bytes, absent_context = self.explicit_decision(
            "2026-12-31T00:00:00Z", manifest_override=before_generation,
            add_queue_additions=False,
        )
        self.assertFalse(any(
            artifact.get("path") in queue_paths for artifact in absent_context["manifest"]["artifacts"]
        ))
        with tempfile.TemporaryDirectory() as raw:
            decision_path = pathlib.Path(raw) / "decision.json"
            decision_path.write_bytes(absent_decision_bytes)
            with self.assertRaisesRegex(ValueError, "every independently authorized addition"):
                MODULE.expected(
                    policy, absent_context["manifest"], absent_context["compatibility"], decision_path,
                    scope_evaluation=absent_context["evaluation"],
                )

    def test_unchanged_historical_decision_approves_exact_artifact_only_rebinding(self):
        payloads = self.historical_scope_fixture_payloads()
        manifest = self.historical_manifest_with_authorized_additions()
        policy = json.loads((ROOT / "policy/health-observation-plan-technical-rebinding.json").read_bytes())
        with tempfile.TemporaryDirectory() as raw:
            fixture_dir = pathlib.Path(raw) / "manual-review-scope"
            fixture_dir.mkdir()
            for name, content in payloads.items():
                (fixture_dir / name).write_bytes(content)
            decision_path = fixture_dir / "decision.json"
            decision_bytes = payloads["decision.json"]
            decision = json.loads(decision_bytes)
            compatibility = json.loads(payloads["compatibility.json"])
            handoff = json.loads(payloads["handoff.json"])
            health_plan = json.loads(payloads["health-plan.json"])
            health_selection = json.loads(payloads["health-selection.json"])
            scope = manual_review_scope.evaluate_review_scope(
                decision=decision,
                decision_sha256=hashlib.sha256(decision_bytes).hexdigest(),
                compatibility=compatibility,
                handoff=handoff,
                handoff_sha256=hashlib.sha256(payloads["handoff.json"]).hexdigest(),
                manifest=manifest,
                health_plan=health_plan,
                health_selection=health_selection,
                as_of=dt.datetime(2026, 7, 23, tzinfo=dt.timezone.utc),
                fixture_dir=fixture_dir,
            )
            self.assertTrue(scope["historical_decision"])
            self.assertEqual(scope["scope_status"], "unchanged")
            self.assertTrue(scope["effective_accepted"])
            rebinding = MODULE.expected(
                policy,
                manifest,
                compatibility,
                decision_path,
                scope_evaluation=scope,
            )

        self.assertEqual(rebinding["status"], "approved_artifact_only_rebinding")
        self.assertTrue(rebinding["historical_rebinding_eligible"])
        self.assertEqual(rebinding["historical_decision_sha256"], policy["decision_sha256"])
        self.assertEqual(rebinding["manifest_delta"]["artifact_count_before"], 161)
        self.assertEqual(rebinding["manifest_delta"]["artifact_count_after"], 205)
        self.assertEqual(
            rebinding["manifest_delta"]["stripped_artifact_contract_sha256"],
            policy["baseline"]["artifact_contract_sha256"],
        )

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
