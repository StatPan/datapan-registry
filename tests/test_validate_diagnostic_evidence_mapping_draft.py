import copy
import hashlib
import importlib.util
import json
import pathlib
import subprocess
import sys
import tempfile
import unittest
from unittest import mock


ROOT = pathlib.Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("diagnostic_mapping", ROOT / "scripts/validate-diagnostic-evidence-mapping-draft.py")
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC.loader
SPEC.loader.exec_module(MODULE)


class DiagnosticEvidenceMappingDraftTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.mapping = MODULE.load(MODULE.MAPPING)
        cls.contract = MODULE.load(MODULE.CONTRACT)

    def fixture_instances(self, name, kinds=None):
        value = MODULE.load(MODULE.FIXTURES / f"{name}.json")
        evidence = value["evidence_refs"]
        if kinds:
            evidence = [item for item in evidence if item["kind"] in kinds]
        return value["subject"], [{"subject": copy.deepcopy(value["subject"]), "evidence": copy.deepcopy(item)} for item in evidence]

    def candidate_instance(self, predicate_id, subject):
        predicate = self.mapping["evidence_predicates"][predicate_id]
        if predicate["kind"] == "registry_rule":
            ref_id = predicate["matches"]["/ref_id"]
            evidence = {
                "kind": "registry_rule", "ref_id": ref_id, "authority": "registry",
                "timing": {"basis": "relative_to_assessed_at", "observed_age_seconds": 0, "validity": "current_at_assessment", "validity_policy_version": "fixture-v1", "remaining_validity_seconds": 60},
                "scope": {"level": "operation", "subject_ref": "envelope_subject"}, "supports": ["scope"],
            }
        else:
            _, instances = self.fixture_instances("provider-outage", {"provider_response"})
            evidence = instances[0]["evidence"]
            for pointer, value in predicate["matches"].items():
                MODULE.pointer_set(evidence, pointer, value)
        return {"subject": copy.deepcopy(subject), "evidence": evidence}

    def test_checked_in_typed_mapping_packets_and_proofs(self):
        self.assertEqual(MODULE.validate_all(), {"predicates": 25, "causes": 11, "proof_cases": 9, "consumers": 3})

    def test_registry_identity_proof_digest_is_pinned(self):
        with mock.patch.object(MODULE, "EXPECTED_REGISTRY_IDENTITY_PROOF_SHA256", "0" * 64):
            with self.assertRaisesRegex(ValueError, "identity proof digest drift"):
                MODULE.validate_registry_identity_proof(self.mapping, MODULE.REGISTRY_IDENTITY_PROOF)

    def test_registry_identity_proof_rejects_synchronized_source_semantic_drift(self):
        proof = MODULE.load(MODULE.REGISTRY_IDENTITY_PROOF)
        proof["datasets"][0]["operations"][0]["source_system"] = "forged.example"
        with tempfile.TemporaryDirectory() as directory:
            path = pathlib.Path(directory) / "proof.json"
            path.write_text(json.dumps(proof), encoding="utf-8")
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            with mock.patch.object(MODULE, "REGISTRY_IDENTITY_PROOF", path), mock.patch.object(MODULE, "EXPECTED_REGISTRY_IDENTITY_PROOF_SHA256", digest):
                with self.assertRaisesRegex(ValueError, "source semantics drift"):
                    MODULE.validate_registry_identity_proof(self.mapping, path)

    def test_cli_entrypoint(self):
        result = subprocess.run([sys.executable, str(ROOT / "scripts/validate-diagnostic-evidence-mapping-draft.py")], capture_output=True, text=True, check=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("proof_cases=9", result.stdout)

    def test_every_candidate_is_structurally_denied_as_selector(self):
        subject, _ = self.fixture_instances("unknown")
        candidate_ids = [predicate_id for predicate_id, predicate in self.mapping["evidence_predicates"].items() if MODULE.intrinsically_candidate(predicate)]
        self.assertEqual(set(candidate_ids), {"rule_code30", "rule_service_key_message", "generic_http_401", "generic_http_403", "generic_http_404", "rule_timeout", "rule_404", "rule_parse"})
        for predicate_id in candidate_ids:
            with self.subTest(predicate_id=predicate_id):
                value = copy.deepcopy(self.mapping)
                target = next(item for item in value["cause_mappings"] if item["cause"] == "credential_invalid")
                target["selector_groups"] = [[predicate_id]]
                target["corroborator_groups"] = []
                result = MODULE.resolve(value, subject, [self.candidate_instance(predicate_id, subject)])
                self.assertEqual(result["cause"], "unknown")
                with self.assertRaisesRegex(ValueError, "intrinsically non-selecting predicate used as selector"):
                    MODULE.validate_mapping(value, self.contract)

    def test_rule_parse_remains_non_selecting_without_a_mutable_declaration_list(self):
        value = copy.deepcopy(self.mapping)
        self.assertNotIn("candidate_only_predicates", value)
        target = next(item for item in value["cause_mappings"] if item["cause"] == "provider_outage")
        target["selector_groups"] = [["rule_parse"]]
        subject, _ = self.fixture_instances("unknown")
        instance = self.candidate_instance("rule_parse", subject)
        self.assertEqual(MODULE.resolve(value, subject, [instance])["cause"], "unknown")
        with self.assertRaisesRegex(ValueError, "intrinsically non-selecting"):
            MODULE.validate_mapping(value, self.contract)

    def test_empty_or_scope_only_support_can_never_select(self):
        for supports in ([], ["scope"]):
            with self.subTest(supports=supports):
                value = copy.deepcopy(self.mapping)
                value["evidence_predicates"]["health_unavailable"]["supports"] = supports
                self.assertTrue(MODULE.intrinsically_candidate(value["evidence_predicates"]["health_unavailable"]))
                with self.assertRaisesRegex(ValueError, "intrinsically non-selecting"):
                    MODULE.validate_mapping(value, self.contract)

    def test_same_operation_current_approval_resolves_but_cross_operation_does_not(self):
        case = next(item for item in self.mapping["proof_cases"] if item["case_id"] == "approval-same-operation-current")
        subject, instances = MODULE.proof_instances(case)
        self.assertEqual(MODULE.resolve(self.mapping, subject, instances)["cause"], "approval_propagating")
        instances[0]["subject"]["operation_id"] = "another-operation"
        instances[1]["subject"]["operation_id"] = "another-operation"
        self.assertEqual(MODULE.resolve(self.mapping, subject, instances)["cause"], "unknown")

    def test_approval_required_routes_only_for_exact_registry_dataset(self):
        subject, instances = self.fixture_instances("approval-required")
        result = MODULE.resolve(self.mapping, subject, instances)
        self.assertEqual(result["cause"], "unknown")
        self.assertEqual(result["recommended_action"], "gather_more_evidence")
        subject["dataset_id"] = "15000017"
        for instance in instances:
            instance["subject"] = copy.deepcopy(subject)
        result = MODULE.resolve(self.mapping, subject, instances)
        self.assertEqual(result["cause"], "approval_required")
        self.assertEqual(result["application_entry"], {"kind": "dataset_application_entry", "url": "https://www.data.go.kr/data/15000017/openapi.do", "direct_submission_url": False})
        other_source = copy.deepcopy(subject)
        other_source["source_id"] = "other_source"
        other_instances = copy.deepcopy(instances)
        for instance in other_instances:
            instance["subject"] = copy.deepcopy(other_source)
        self.assertEqual(MODULE.resolve(self.mapping, other_source, other_instances)["cause"], "unknown")
        for invalid in ("../../etc", "15000018x"):
            bad_subject = copy.deepcopy(subject)
            bad_subject["dataset_id"] = invalid
            bad_instances = copy.deepcopy(instances)
            for instance in bad_instances:
                instance["subject"] = copy.deepcopy(bad_subject)
            self.assertEqual(MODULE.resolve(self.mapping, bad_subject, bad_instances)["cause"], "unknown")

    def test_stale_expired_and_wrong_authority_evidence_are_excluded(self):
        subject, instances = self.fixture_instances("provider-outage", {"health_observation"})
        for mutation in (
            ("/timing/observed_age_seconds", 901),
            ("/timing/remaining_validity_seconds", 0),
            ("/authority", "provider"),
        ):
            with self.subTest(mutation=mutation):
                value = copy.deepcopy(instances)
                MODULE.pointer_set(value[0]["evidence"], *mutation)
                self.assertEqual(MODULE.resolve(self.mapping, subject, value)["cause"], "unknown")

    def test_health_notice_and_response_outage_variants(self):
        expectations = {
            "health-outage": ("inferred", ["reissue_credential"]),
            "notice-outage": ("observed", ["reissue_credential"]),
            "response-only-outage": ("inferred", []),
        }
        for case_id, (determination, avoid) in expectations.items():
            case = next(item for item in self.mapping["proof_cases"] if item["case_id"] == case_id)
            subject, instances = MODULE.proof_instances(case)
            result = MODULE.resolve(self.mapping, subject, instances)
            self.assertEqual(result["cause"], "provider_outage")
            self.assertEqual(result["determination"], determination)
            self.assertEqual(result["avoid_actions"], avoid)

    def test_conflicting_typed_causes_fall_back_to_unknown(self):
        subject, credential = self.fixture_instances("credential-invalid")
        _, rate = self.fixture_instances("rate-limited")
        rate[0]["subject"] = copy.deepcopy(subject)
        self.assertEqual(MODULE.resolve(self.mapping, subject, credential + rate)["cause"], "unknown")

    def test_request_validation_requires_full_envelope_payload(self):
        subject, instances = self.fixture_instances("invalid-input")
        self.assertEqual(MODULE.resolve(self.mapping, subject, instances)["cause"], "invalid_input")
        for pointer in ("/request_validation/result", "/request_validation/policy_version"):
            value = copy.deepcopy(instances)
            container = value[0]["evidence"]["request_validation"]
            container.pop(pointer.rsplit("/", 1)[1])
            self.assertEqual(MODULE.resolve(self.mapping, subject, value)["cause"], "unknown")

    def test_ready_requires_achieved_level_to_satisfy_required_level(self):
        subject, instances = self.fixture_instances("ready")
        self.assertEqual(MODULE.resolve(self.mapping, subject, instances)["cause"], "ready")
        instances[0]["evidence"]["validation"]["required_level"] = "L4"
        instances[0]["evidence"]["validation"]["achieved_level"] = "L1"
        self.assertEqual(MODULE.resolve(self.mapping, subject, instances)["cause"], "unknown")

    def test_unknown_kind_field_scope_and_authority_predicates_fail(self):
        mutations = [
            ("kind", "made_up"),
            ("matches", {"/response/made_up": "x"}),
            ("matches", {"/response/provider_class": "made_up"}),
            ("scope_level", "source"),
            ("authorities", ["registry"]),
        ]
        for field, value in mutations:
            with self.subTest(field=field):
                mapping = copy.deepcopy(self.mapping)
                mapping["evidence_predicates"]["rate_limited"][field] = value
                with self.assertRaises(ValueError):
                    MODULE.validate_predicates(mapping)

    def test_bogus_or_empty_source_basis_fails(self):
        for basis in ([], [{"type": "registry_fact", "artifact": "sources/data_go_kr.json", "json_pointer": "/missing", "equals": "x"}]):
            with self.subTest(basis=basis):
                mapping = copy.deepcopy(self.mapping)
                mapping["cause_mappings"][0]["source_basis"] = basis
                with self.assertRaises(ValueError):
                    MODULE.validate_source_basis(mapping)

    def test_source_basis_required_values_cannot_be_removed_or_changed(self):
        approval = next(item for item in self.mapping["cause_mappings"] if item["cause"] == "approval_required")
        for index, field, replacement in ((0, "expected", None), (0, "expected", {"rule_id": "wrong"}), (1, "equals", None), (1, "equals", "source")):
            with self.subTest(index=index, field=field, replacement=replacement):
                mapping = copy.deepcopy(self.mapping)
                basis = next(item for item in mapping["cause_mappings"] if item["cause"] == "approval_required")["source_basis"][index]
                if replacement is None:
                    basis.pop(field)
                else:
                    basis[field] = replacement
                with self.assertRaises(ValueError):
                    MODULE.validate_source_basis(mapping)

    def test_packets_reject_digest_dependency_and_obligation_drift(self):
        digest = hashlib.sha256(MODULE.MAPPING.read_bytes()).hexdigest()
        path = MODULE.PACKETS / "datapan-cli.v1.json"
        original = MODULE.load(path)
        for mutation in ("digest", "dependency", "obligation"):
            packet = copy.deepcopy(original)
            if mutation == "digest":
                packet["mapping_contract"]["sha256"] = "0" * 64
            elif mutation == "dependency":
                packet["production_status"]["required_after_dependencies"] = ["arbitrary"]
            else:
                packet["obligations"]["action"] = "arbitrary text"
            real_load = MODULE.load
            with mock.patch.object(MODULE, "load", side_effect=lambda candidate, packet=packet: packet if candidate == path else real_load(candidate)):
                with self.assertRaises(ValueError):
                    MODULE.validate_packets(digest)

    def test_packets_reject_schema_top_level_and_sensitive_key_drift(self):
        digest = hashlib.sha256(MODULE.MAPPING.read_bytes()).hexdigest()
        path = MODULE.PACKETS / "datapan-health.v1.json"
        original = MODULE.load(path)
        self.assertIn("StatPan/datapan-health#20", original["production_status"]["required_after_dependencies"])
        for mutation in ("schema", "top_level", "sensitive"):
            packet = copy.deepcopy(original)
            if mutation == "schema":
                packet["schema_version"] = "made-up"
            elif mutation == "top_level":
                packet["extra"] = True
            else:
                packet["obligations"]["credential"] = "fixture"
            real_load = MODULE.load
            with mock.patch.object(MODULE, "load", side_effect=lambda candidate, packet=packet: packet if candidate == path else real_load(candidate)):
                with self.assertRaises(ValueError):
                    MODULE.validate_packets(digest)


if __name__ == "__main__":
    unittest.main()
