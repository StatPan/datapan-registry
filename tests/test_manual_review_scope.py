from __future__ import annotations

import copy
import datetime as dt
import hashlib
import importlib.util
import json
import pathlib
import shutil
import sys
import tempfile
import unittest
import jsonschema
from unittest import mock


ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import manual_review_scope as scope

ACCEPTANCE_GENERATOR_SPEC = importlib.util.spec_from_file_location(
    "generate_manual_review_acceptance",
    ROOT / "scripts/generate-credential-runtime-manual-review-acceptance.py",
)
assert ACCEPTANCE_GENERATOR_SPEC is not None and ACCEPTANCE_GENERATOR_SPEC.loader is not None
acceptance_generator = importlib.util.module_from_spec(ACCEPTANCE_GENERATOR_SPEC)
sys.modules[ACCEPTANCE_GENERATOR_SPEC.name] = acceptance_generator
ACCEPTANCE_GENERATOR_SPEC.loader.exec_module(acceptance_generator)
PACKET_GENERATOR_SPEC = importlib.util.spec_from_file_location(
    "generate_manual_review_acceptance_packet",
    ROOT / "scripts/generate-credential-runtime-manual-review-acceptance-packet.py",
)
assert PACKET_GENERATOR_SPEC is not None and PACKET_GENERATOR_SPEC.loader is not None
packet_generator = importlib.util.module_from_spec(PACKET_GENERATOR_SPEC)
sys.modules[PACKET_GENERATOR_SPEC.name] = packet_generator
PACKET_GENERATOR_SPEC.loader.exec_module(packet_generator)
DECISION_VALIDATOR_SPEC = importlib.util.spec_from_file_location(
    "validate_manual_review_decision",
    ROOT / "scripts/validate-credential-runtime-manual-review-decision.py",
)
assert DECISION_VALIDATOR_SPEC is not None and DECISION_VALIDATOR_SPEC.loader is not None
decision_validator = importlib.util.module_from_spec(DECISION_VALIDATOR_SPEC)
sys.modules[DECISION_VALIDATOR_SPEC.name] = decision_validator
DECISION_VALIDATOR_SPEC.loader.exec_module(decision_validator)


def load(path: str) -> dict:
    return json.loads((ROOT / path).read_text(encoding="utf-8"))


class ManualReviewScopeTest(unittest.TestCase):
    def setUp(self) -> None:
        self.decision = load("reports/credential-runtime-manual-review-decision.json")
        self.acceptance = load("reports/credential-runtime-manual-review-acceptance.json")
        self.compatibility = load("reports/release-consumer-compatibility.json")
        self.historical_proof = scope._load_historical()
        self.historical_compatibility = self.historical_proof["compatibility.json"]
        self.historical_manifest = self.historical_proof["manifest.json"]
        self.historical_handoff = self.historical_proof["handoff.json"]
        self.historical_handoff_bytes = (scope.DEFAULT_FIXTURE_DIR / "handoff.json").read_bytes()
        self.historical_health_plan = self.historical_proof["health-plan.json"]
        self.historical_health_selection = self.historical_proof["health-selection.json"]
        self.handoff = load("reports/credential-runtime-review-handoff.json")
        self.manifest = load("manifest.json")
        self.health_plan = load("reports/health-runtime-observation-plan.v1.json")
        self.health_selection = load("policy/health-runtime-observation-selection.json")
        self.decision_bytes = (ROOT / "reports/credential-runtime-manual-review-decision.json").read_bytes()
        self.handoff_bytes = (ROOT / "reports/credential-runtime-review-handoff.json").read_bytes()

    def build_current_packet(self) -> dict:
        return packet_generator.build_report(
            self.decision,
            self.acceptance,
            self.handoff,
            self.compatibility,
            decision_path=pathlib.Path("reports/credential-runtime-manual-review-decision.json"),
            acceptance_path=pathlib.Path("reports/credential-runtime-manual-review-acceptance.json"),
            handoff_path=pathlib.Path("reports/credential-runtime-review-handoff.json"),
            compatibility_path=pathlib.Path("reports/release-consumer-compatibility.json"),
        )

    def decision_from_packet_template(self, packet: dict) -> dict:
        decision = copy.deepcopy(self.decision)
        body = copy.deepcopy(packet["accepted_decision_template"])
        body.update(
            reviewer="Test Accountable Reviewer",
            reviewed_at="2026-09-30T12:00:00Z",
            reason="Synthetic test review bound to the current manual-review scope.",
            expires_at="2026-11-30T00:00:00Z",
        )
        decision["decision"] = body
        return decision

    def validate_test_decision(self, decision: dict, raw: pathlib.Path, *, as_of: dt.datetime) -> dict:
        raw.write_text(json.dumps(decision, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        decision_validator.validate_schema(
            decision,
            ROOT / "schemas/datapan.credential-runtime-manual-review-decision.v1.schema.json",
        )
        evaluate_review_scope = decision_validator.evaluate_review_scope

        def evaluate_at_test_clock(**kwargs):
            return evaluate_review_scope(**kwargs, as_of=as_of)

        with mock.patch.object(
            decision_validator,
            "evaluate_review_scope",
            side_effect=evaluate_at_test_clock,
        ):
            return decision_validator.validate_decision(
                decision,
                decision_path=raw,
                handoff_path=pathlib.Path("reports/credential-runtime-review-handoff.json"),
                compatibility_path=pathlib.Path("reports/release-consumer-compatibility.json"),
                manifest_path=ROOT / "manifest.json",
                health_plan_path=ROOT / "reports/health-runtime-observation-plan.v1.json",
                health_selection_path=ROOT / "policy/health-runtime-observation-selection.json",
            )

    def evaluate(
        self,
        *,
        decision: dict | None = None,
        decision_bytes: bytes | None = None,
        compatibility: dict | None = None,
        manifest: dict | None = None,
        handoff: dict | None = None,
        handoff_bytes: bytes | None = None,
        health_plan: dict | None = None,
        health_selection: dict | None = None,
        as_of: dt.datetime | None = None,
        fixture_dir: pathlib.Path = scope.DEFAULT_FIXTURE_DIR,
    ) -> dict:
        decision = decision if decision is not None else self.decision
        decision_bytes = decision_bytes if decision_bytes is not None else self.decision_bytes
        compatibility = compatibility if compatibility is not None else self.compatibility
        manifest = manifest if manifest is not None else self.manifest
        handoff = handoff if handoff is not None else self.handoff
        handoff_bytes = handoff_bytes if handoff_bytes is not None else self.handoff_bytes
        return scope.evaluate_review_scope(
            decision=decision,
            decision_sha256=hashlib.sha256(decision_bytes).hexdigest(),
            compatibility=compatibility,
            handoff=handoff,
            handoff_sha256=hashlib.sha256(handoff_bytes).hexdigest(),
            manifest=manifest,
            health_plan=health_plan if health_plan is not None else self.health_plan,
            health_selection=health_selection if health_selection is not None else self.health_selection,
            as_of=as_of,
            fixture_dir=fixture_dir,
        )

    def evaluate_historical(self, **kwargs) -> dict:
        inputs = {
            "compatibility": self.historical_compatibility,
            "manifest": self.historical_manifest,
            "handoff": self.historical_handoff,
            "handoff_bytes": self.historical_handoff_bytes,
            "health_plan": self.historical_health_plan,
            "health_selection": self.historical_health_selection,
        }
        inputs.update(kwargs)
        return self.evaluate(**inputs)

    @staticmethod
    def before_expiry() -> dt.datetime:
        return dt.datetime(2026, 8, 15, 10, 29, 59, tzinfo=dt.timezone.utc)

    def test_pinned_scope_is_accepted_before_expiry_and_pending_at_expiry(self):
        before = self.evaluate_historical(as_of=self.before_expiry())
        at = self.evaluate_historical(
            as_of=dt.datetime(2026, 8, 15, 10, 30, tzinfo=dt.timezone.utc),
        )

        self.assertEqual(before["scope_status"], "unchanged")
        self.assertTrue(before["effective_accepted"])
        self.assertEqual(at["scope_status"], "revalidation_required")
        self.assertFalse(at["effective_accepted"])
        self.assertIn("manual_review_decision_expired", at["reason_codes"])

    def test_scope_schema_is_strict_and_embedded_acceptance_contract_stays_in_sync(self):
        standalone = load("schemas/datapan.manual-review-scope.v1.schema.json")
        acceptance_schema = load("schemas/datapan.credential-runtime-manual-review-acceptance.v1.schema.json")
        embedded = acceptance_schema["$defs"]["review_scope"]
        expected_embedded = {
            key: copy.deepcopy(value)
            for key, value in standalone.items()
            if key not in {"$schema", "$id", "title", "description"}
        }

        def rewrite_scope_refs(value):
            if isinstance(value, dict):
                for key, child in list(value.items()):
                    if key == "$ref" and isinstance(child, str) and child.startswith("#/$defs/"):
                        value[key] = "#/$defs/review_scope/$defs" + child[len("#/$defs"):]
                    else:
                        rewrite_scope_refs(child)
            elif isinstance(value, list):
                for child in value:
                    rewrite_scope_refs(child)

        rewrite_scope_refs(expected_embedded)
        self.assertEqual(embedded, expected_embedded)
        validator = jsonschema.Draft202012Validator(standalone)
        unchanged = self.evaluate_historical(as_of=self.before_expiry())
        expired = self.evaluate_historical(
            as_of=dt.datetime(2026, 8, 15, 10, 30, tzinfo=dt.timezone.utc),
        )
        validator.validate(unchanged)
        validator.validate(expired)
        invalid_pending = copy.deepcopy(expired)
        invalid_pending["effective_accepted"] = True
        self.assertTrue(list(validator.iter_errors(invalid_pending)))
        invalid_timestamp = copy.deepcopy(expired)
        invalid_timestamp["decision_expires_at"] = "2026-08-15T10:30:00-07:00"
        self.assertTrue(list(validator.iter_errors(invalid_timestamp)))

    def test_source_risk_and_health_changes_require_revalidation(self):
        changed_manifest = copy.deepcopy(self.manifest)
        entry = next(item for item in changed_manifest["artifacts"] if item["path"] == scope.HISTORICAL_SOURCE_PATH)
        entry["sha256"] = "a" * 64
        source = self.evaluate(manifest=changed_manifest, as_of=self.before_expiry())
        self.assertFalse(source["effective_accepted"])
        self.assertIn("source_registry_changed", source["reason_codes"])

        changed_compatibility = copy.deepcopy(self.compatibility)
        changed_compatibility["runtime_risk_evidence"]["credential_handoff_relief_decision"] = "unreviewed_change"
        risk = self.evaluate(compatibility=changed_compatibility, as_of=self.before_expiry())
        self.assertFalse(risk["effective_accepted"])
        self.assertIn("runtime_risk_semantics_changed", risk["reason_codes"])

        changed_health = copy.deepcopy(self.health_plan)
        changed_health["shards"][0]["members"][0]["endpoint"]["path"] += "/changed"
        health = self.evaluate(health_plan=changed_health, as_of=self.before_expiry())
        self.assertFalse(health["effective_accepted"])
        self.assertIn("health_observation_scope_changed", health["reason_codes"])

    def test_other_compatibility_semantics_are_also_bound(self):
        changed = copy.deepcopy(self.historical_compatibility)
        changed["provider"] = "different-provider"
        evaluated = self.evaluate_historical(compatibility=changed, as_of=self.before_expiry())

        self.assertEqual(evaluated["scope_status"], "revalidation_required")
        self.assertFalse(evaluated["effective_accepted"])
        self.assertIn("compatibility_scope_changed", evaluated["reason_codes"])

    def test_only_derived_acceptance_outputs_do_not_change_review_scope(self):
        changed = copy.deepcopy(self.historical_compatibility)
        for key in scope.DERIVED_ACCEPTANCE_FIELDS:
            changed["runtime_risk_evidence"][key] = f"derived-{key}"

        baseline = self.evaluate_historical(as_of=self.before_expiry())
        updated = self.evaluate_historical(compatibility=changed, as_of=self.before_expiry())

        self.assertEqual(
            baseline["current_binding"]["review_scope_sha256"],
            updated["current_binding"]["review_scope_sha256"],
        )
        self.assertEqual(updated["scope_status"], "unchanged")
        self.assertTrue(updated["effective_accepted"])

    def test_decision_mirrors_do_not_change_scope_but_other_risk_facts_remain_bound(self):
        changed = copy.deepcopy(self.historical_compatibility)
        for key in scope.DECISION_MIRROR_FIELDS:
            changed["runtime_risk_evidence"][key] = f"new-human-assertion-{key}"

        baseline = self.evaluate_historical(as_of=self.before_expiry())
        mirrored = self.evaluate_historical(compatibility=changed, as_of=self.before_expiry())
        self.assertEqual(
            baseline["current_binding"]["review_scope_sha256"],
            mirrored["current_binding"]["review_scope_sha256"],
        )
        self.assertEqual(mirrored["scope_status"], "unchanged")
        self.assertTrue(mirrored["effective_accepted"])

        changed["runtime_risk_evidence"]["manual_review_required"] = not bool(
            changed["runtime_risk_evidence"]["manual_review_required"]
        )
        risk_changed = self.evaluate_historical(compatibility=changed, as_of=self.before_expiry())
        self.assertFalse(risk_changed["effective_accepted"])
        self.assertIn("runtime_risk_semantics_changed", risk_changed["reason_codes"])

    def test_new_decision_requires_versioned_current_scope_digest(self):
        new_decision = copy.deepcopy(self.decision)
        body = new_decision["decision"]
        body["reviewer"] = "Accountable reviewer"
        body["reviewed_at"] = "2026-08-01T10:00:00Z"
        body["expires_at"] = "2027-01-01T00:00:00Z"
        body["review_scope_version"] = scope.REVIEW_SCOPE_VERSION
        body["handoff_sha256"] = hashlib.sha256(self.handoff_bytes).hexdigest()
        body["review_scope_sha256"] = scope.review_scope_sha256(
            compatibility=self.compatibility,
            handoff_sha256=hashlib.sha256(self.handoff_bytes).hexdigest(),
            manifest=self.manifest,
            health_plan=self.health_plan,
            health_selection=self.health_selection,
        )
        decision_bytes = (json.dumps(new_decision, ensure_ascii=False, indent=2) + "\n").encode()
        accepted = self.evaluate(
            decision=new_decision,
            decision_bytes=decision_bytes,
            as_of=dt.datetime(2026, 9, 1, tzinfo=dt.timezone.utc),
        )
        self.assertEqual(accepted["scope_status"], "explicitly_revalidated")
        self.assertTrue(accepted["effective_accepted"])

        at_review_time = self.evaluate(
            decision=new_decision,
            decision_bytes=decision_bytes,
            as_of=dt.datetime(2026, 8, 1, 10, 0, tzinfo=dt.timezone.utc),
        )
        self.assertEqual(at_review_time["scope_status"], "explicitly_revalidated")
        self.assertTrue(at_review_time["effective_accepted"])

        body["review_scope_sha256"] = "0" * 64
        invalid_bytes = (json.dumps(new_decision, ensure_ascii=False, indent=2) + "\n").encode()
        pending = self.evaluate(
            decision=new_decision,
            decision_bytes=invalid_bytes,
            as_of=dt.datetime(2026, 9, 1, tzinfo=dt.timezone.utc),
        )
        self.assertEqual(pending["scope_status"], "unproven")
        self.assertFalse(pending["effective_accepted"])

    def test_new_review_cannot_be_future_dated_or_change_other_compatibility_semantics(self):
        new_decision = copy.deepcopy(self.decision)
        body = new_decision["decision"]
        body.update(
            reviewer="Accountable reviewer",
            reviewed_at="2026-10-02T00:00:00Z",
            expires_at="2027-01-01T00:00:00Z",
            review_scope_version=scope.REVIEW_SCOPE_VERSION,
            handoff_sha256=hashlib.sha256(self.handoff_bytes).hexdigest(),
        )
        body["review_scope_sha256"] = scope.review_scope_sha256(
            compatibility=self.compatibility,
            handoff_sha256=hashlib.sha256(self.handoff_bytes).hexdigest(),
            manifest=self.manifest,
            health_plan=self.health_plan,
            health_selection=self.health_selection,
        )
        decision_bytes = (json.dumps(new_decision, ensure_ascii=False, indent=2) + "\n").encode()
        with self.assertRaisesRegex(ValueError, "future"):
            self.evaluate(
                decision=new_decision,
                decision_bytes=decision_bytes,
                as_of=dt.datetime(2026, 10, 1, tzinfo=dt.timezone.utc),
            )

        body["reviewed_at"] = "not-a-timestamp"
        malformed_bytes = (json.dumps(new_decision, ensure_ascii=False, indent=2) + "\n").encode()
        with self.assertRaisesRegex(ValueError, "ISO-8601 UTC"):
            self.evaluate(
                decision=new_decision,
                decision_bytes=malformed_bytes,
                as_of=dt.datetime(2026, 10, 1, tzinfo=dt.timezone.utc),
            )

        changed_compatibility = copy.deepcopy(self.compatibility)
        changed_compatibility["provider"] = "different-provider"
        body["reviewed_at"] = "2026-09-01T00:00:00Z"
        decision_bytes = (json.dumps(new_decision, ensure_ascii=False, indent=2) + "\n").encode()
        pending = self.evaluate(
            decision=new_decision,
            decision_bytes=decision_bytes,
            compatibility=changed_compatibility,
            as_of=dt.datetime(2026, 10, 1, tzinfo=dt.timezone.utc),
        )
        self.assertEqual(pending["scope_status"], "unproven")
        self.assertFalse(pending["effective_accepted"])

    def test_explicit_revalidation_acceptance_report_uses_accepted_status(self):
        new_decision = copy.deepcopy(self.decision)
        body = new_decision["decision"]
        body.update(
            reviewer="Accountable reviewer",
            reviewed_at="2026-09-01T00:00:00Z",
            expires_at="2027-01-01T00:00:00Z",
            review_scope_version=scope.REVIEW_SCOPE_VERSION,
            handoff_sha256=hashlib.sha256(self.handoff_bytes).hexdigest(),
        )
        body["review_scope_sha256"] = scope.review_scope_sha256(
            compatibility=self.compatibility,
            handoff_sha256=hashlib.sha256(self.handoff_bytes).hexdigest(),
            manifest=self.manifest,
            health_plan=self.health_plan,
            health_selection=self.health_selection,
        )

        with tempfile.TemporaryDirectory() as raw:
            decision_path = pathlib.Path(raw) / "decision.json"
            decision_path.write_text(json.dumps(new_decision, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            decision_validator.validate_schema(
                new_decision,
                ROOT / "schemas/datapan.credential-runtime-manual-review-decision.v1.schema.json",
            )
            validated_scope = decision_validator.validate_decision(
                new_decision,
                decision_path=decision_path,
                handoff_path=pathlib.Path("reports/credential-runtime-review-handoff.json"),
                compatibility_path=pathlib.Path("reports/release-consumer-compatibility.json"),
                manifest_path=ROOT / "manifest.json",
                health_plan_path=ROOT / "reports/health-runtime-observation-plan.v1.json",
                health_selection_path=ROOT / "policy/health-runtime-observation-selection.json",
            )
            self.assertEqual(validated_scope["scope_status"], "explicitly_revalidated")
            report = acceptance_generator.build_report(
                self.handoff,
                self.compatibility,
                new_decision,
                handoff_path=ROOT / "reports/credential-runtime-review-handoff.json",
                compatibility_path=ROOT / "reports/release-consumer-compatibility.json",
                decision_path=decision_path,
                manifest=self.manifest,
                health_plan=self.health_plan,
                health_selection=self.health_selection,
                as_of=dt.datetime(2026, 10, 1, tzinfo=dt.timezone.utc),
            )
            report["inputs"]["credential_runtime_manual_review_decision"] = (
                "reports/credential-runtime-manual-review-decision.json"
            )
            report["inputs"]["credential_runtime_review_handoff"] = (
                "reports/credential-runtime-review-handoff.json"
            )
            report["inputs"]["release_consumer_compatibility"] = (
                "reports/release-consumer-compatibility.json"
            )
            report["acceptance_routing"]["decision_intake_path"] = (
                "reports/credential-runtime-manual-review-decision.json"
            )
            acceptance_generator.validate_schema(
                report,
                ROOT / "schemas/datapan.credential-runtime-manual-review-acceptance.v1.schema.json",
            )

        self.assertTrue(report["summary"]["accepted"])
        self.assertEqual(report["summary"]["acceptance_status"], "accepted")
        self.assertEqual(report["review_scope"]["scope_status"], "explicitly_revalidated")

    def test_packet_cli_template_satisfies_real_decision_consumer(self):
        with tempfile.TemporaryDirectory() as raw:
            packet_path = pathlib.Path(raw) / "packet.json"
            with mock.patch.object(
                sys,
                "argv",
                [
                    "generate-credential-runtime-manual-review-acceptance-packet.py",
                    "--output",
                    str(packet_path),
                ],
            ):
                self.assertEqual(packet_generator.main(), 0)
            packet = json.loads(packet_path.read_text(encoding="utf-8"))

            expected_triggers = [
                "credential_receipt_state_changes",
                "consumer_compatibility_changes",
                "source_runtime_blocker_changes",
                "release_manifest_changes",
                "acceptance_expiry",
            ]
            self.assertEqual(packet["revalidation_triggers"], expected_triggers)
            self.assertEqual(packet["accepted_decision_template"]["revalidation_triggers"], expected_triggers)

            # The generated template is consumed by the actual full decision
            # schema and semantic validator using this checkout's real inputs.
            synthetic = self.decision_from_packet_template(packet)
            decision_path = pathlib.Path(raw) / "synthetic-decision.json"
            accepted = self.validate_test_decision(
                synthetic,
                decision_path,
                as_of=dt.datetime(2026, 10, 1, tzinfo=dt.timezone.utc),
            )
            self.assertEqual(accepted["scope_status"], "explicitly_revalidated")
            self.assertTrue(accepted["effective_accepted"])

            # --check follows the same producer/consumer contract before it
            # compares the temporary generated bytes.
            with mock.patch.object(
                sys,
                "argv",
                [
                    "generate-credential-runtime-manual-review-acceptance-packet.py",
                    "--output",
                    str(packet_path),
                    "--check",
                ],
            ):
                self.assertEqual(packet_generator.main(), 0)

        self.assertEqual(
            hashlib.sha256(self.decision_bytes).hexdigest(),
            "4d0bee81a67d4b49a1ccb022ab6be7a28a76b9613cbd7c2211d5f22ce9d69151",
        )

    def test_packet_trigger_contract_rejects_invalid_lists_and_consumer_drift(self):
        packet = self.build_current_packet()
        packet_generator.validate_invariants(packet)
        packet_generator.validate_schema(
            packet,
            ROOT / "schemas/datapan.credential-runtime-manual-review-acceptance-packet.v1.schema.json",
        )

        invalid_lists = []
        old_names = [
            "credential_runtime_review_handoff_changed",
            "release_consumer_compatibility_changed",
            "reviewed_credential_receipt_state_changed",
            "release_consumer_decision_changed",
        ]
        wrong = copy.deepcopy(packet)
        wrong["revalidation_triggers"] = old_names
        wrong["accepted_decision_template"]["revalidation_triggers"] = old_names
        invalid_lists.append(wrong)

        omitted = copy.deepcopy(packet)
        omitted["revalidation_triggers"] = omitted["revalidation_triggers"][:-1]
        omitted["accepted_decision_template"]["revalidation_triggers"] = omitted["accepted_decision_template"]["revalidation_triggers"][:-1]
        invalid_lists.append(omitted)

        duplicated = copy.deepcopy(packet)
        duplicated["revalidation_triggers"] = duplicated["revalidation_triggers"] + [duplicated["revalidation_triggers"][0]]
        duplicated["accepted_decision_template"]["revalidation_triggers"] = (
            duplicated["accepted_decision_template"]["revalidation_triggers"]
            + [duplicated["accepted_decision_template"]["revalidation_triggers"][0]]
        )
        invalid_lists.append(duplicated)

        disagreeing = copy.deepcopy(packet)
        disagreeing["accepted_decision_template"]["revalidation_triggers"] = old_names
        invalid_lists.append(disagreeing)

        for invalid in invalid_lists:
            with self.subTest(packet_triggers=invalid["revalidation_triggers"]):
                with self.assertRaisesRegex(ValueError, "packet trigger lists must exactly match"):
                    packet_generator.validate_invariants(invalid)

        base_schema = load("schemas/datapan.credential-runtime-manual-review-decision.v1.schema.json")
        consumer_enum = base_schema["$defs"]["decision"]["properties"]["revalidation_triggers"]["items"]["enum"]
        with tempfile.TemporaryDirectory() as raw:
            schema_path = pathlib.Path(raw) / "decision-schema.json"

            reordered_schema = copy.deepcopy(base_schema)
            reordered_schema["$defs"]["decision"]["properties"]["revalidation_triggers"]["items"]["enum"] = list(
                reversed(consumer_enum)
            )
            schema_path.write_text(json.dumps(reordered_schema), encoding="utf-8")
            packet_generator.validate_invariants(packet, decision_schema_path=schema_path)

            drifted_schemas = []
            added = copy.deepcopy(base_schema)
            added["$defs"]["decision"]["properties"]["revalidation_triggers"]["items"]["enum"].append(
                "future_decision_trigger"
            )
            drifted_schemas.append(("addition", added))

            omitted = copy.deepcopy(base_schema)
            omitted["$defs"]["decision"]["properties"]["revalidation_triggers"]["items"]["enum"].pop()
            drifted_schemas.append(("omission", omitted))

            duplicated = copy.deepcopy(base_schema)
            duplicate_enum = duplicated["$defs"]["decision"]["properties"]["revalidation_triggers"]["items"]["enum"]
            duplicate_enum[-1] = duplicate_enum[0]
            drifted_schemas.append(("duplicate", duplicated))

            wrong_type = copy.deepcopy(base_schema)
            wrong_type["$defs"]["decision"]["properties"]["revalidation_triggers"]["items"]["enum"][0] = 7
            drifted_schemas.append(("wrong type", wrong_type))

            wrong_shape = copy.deepcopy(base_schema)
            wrong_shape["$defs"]["decision"]["properties"]["revalidation_triggers"]["items"]["enum"] = "not-an-array"
            drifted_schemas.append(("wrong shape", wrong_shape))

            for label, drifted_schema in drifted_schemas:
                with self.subTest(consumer_schema=label):
                    schema_path.write_text(json.dumps(drifted_schema), encoding="utf-8")
                    with self.assertRaisesRegex(ValueError, "manual-review decision trigger contract drift"):
                        packet_generator.validate_invariants(packet, decision_schema_path=schema_path)

    def test_generated_decision_wrong_scope_or_expiry_remains_ineffective(self):
        packet = self.build_current_packet()
        valid = self.decision_from_packet_template(packet)
        with tempfile.TemporaryDirectory() as raw:
            raw_path = pathlib.Path(raw)

            wrong_scope = copy.deepcopy(valid)
            wrong_scope["decision"]["review_scope_sha256"] = "0" * 64
            wrong_scope_result = self.validate_test_decision(
                wrong_scope,
                raw_path / "wrong-scope.json",
                as_of=dt.datetime(2026, 10, 1, tzinfo=dt.timezone.utc),
            )
            self.assertEqual(wrong_scope_result["scope_status"], "unproven")
            self.assertFalse(wrong_scope_result["effective_accepted"])

            expired = copy.deepcopy(valid)
            expired["decision"]["reviewed_at"] = "2026-08-01T00:00:00Z"
            expired["decision"]["expires_at"] = "2026-09-30T00:00:00Z"
            expired_result = self.validate_test_decision(
                expired,
                raw_path / "expired.json",
                as_of=dt.datetime(2026, 10, 1, tzinfo=dt.timezone.utc),
            )
            self.assertEqual(expired_result["scope_status"], "revalidation_required")
            self.assertFalse(expired_result["effective_accepted"])
            self.assertIn("manual_review_decision_expired", expired_result["reason_codes"])

    def test_expired_decision_packet_stays_pending_and_schema_valid(self):
        acceptance = acceptance_generator.build_report(
            self.handoff,
            self.compatibility,
            self.decision,
            handoff_path=ROOT / "reports/credential-runtime-review-handoff.json",
            compatibility_path=ROOT / "reports/release-consumer-compatibility.json",
            decision_path=ROOT / "reports/credential-runtime-manual-review-decision.json",
            manifest=self.manifest,
            health_plan=self.health_plan,
            health_selection=self.health_selection,
            as_of=dt.datetime(2026, 10, 1, tzinfo=dt.timezone.utc),
        )
        packet = packet_generator.build_report(
            self.decision,
            acceptance,
            self.handoff,
            self.compatibility,
            decision_path=pathlib.Path("reports/credential-runtime-manual-review-decision.json"),
            acceptance_path=pathlib.Path("reports/credential-runtime-manual-review-acceptance.json"),
            handoff_path=pathlib.Path("reports/credential-runtime-review-handoff.json"),
            compatibility_path=pathlib.Path("reports/release-consumer-compatibility.json"),
        )
        packet_generator.validate_invariants(packet)
        packet_generator.validate_schema(
            packet,
            ROOT / "schemas/datapan.credential-runtime-manual-review-acceptance-packet.v1.schema.json",
        )

        self.assertEqual(
            hashlib.sha256(self.decision_bytes).hexdigest(),
            "4d0bee81a67d4b49a1ccb022ab6be7a28a76b9613cbd7c2211d5f22ce9d69151",
        )
        self.assertEqual(acceptance["review_scope"]["scope_status"], "revalidation_required")
        self.assertFalse(acceptance["review_scope"]["effective_accepted"])
        self.assertTrue(acceptance["review_scope"]["decision_expired"])
        self.assertFalse(packet["summary"]["accepted"])
        self.assertEqual(packet["summary"]["acceptance_status"], "revalidation_required")
        self.assertEqual(packet["summary"]["packet_status"], "manual_review_revalidation_required")
        self.assertFalse(packet["summary"]["goal_closure_allowed"])

    def test_pinned_evidence_tampering_is_a_hard_failure(self):
        with tempfile.TemporaryDirectory() as raw:
            fixture_dir = pathlib.Path(raw)
            shutil.copytree(scope.DEFAULT_FIXTURE_DIR, fixture_dir, dirs_exist_ok=True)
            path = fixture_dir / "decision.json"
            data = json.loads(path.read_text(encoding="utf-8"))
            data["decision"]["reason"] += " changed"
            path.write_text(json.dumps(data), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "modified"):
                scope.verify_historical_proof(fixture_dir)


if __name__ == "__main__":
    unittest.main()
