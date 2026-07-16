import copy
import hashlib
import importlib.util
import pathlib
import subprocess
import sys
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
        self.assertEqual(MODULE.validate_all(), {"predicates": 25, "causes": 11, "proof_cases": 8, "consumers": 3})

    def test_cli_entrypoint(self):
        result = subprocess.run([sys.executable, str(ROOT / "scripts/validate-diagnostic-evidence-mapping-draft.py")], capture_output=True, text=True, check=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("proof_cases=8", result.stdout)

    def test_every_candidate_is_structurally_denied_as_selector(self):
        subject, _ = self.fixture_instances("unknown")
        for predicate_id in self.mapping["candidate_only_predicates"]:
            with self.subTest(predicate_id=predicate_id):
                value = copy.deepcopy(self.mapping)
                target = next(item for item in value["cause_mappings"] if item["cause"] == "credential_invalid")
                target["selector_groups"] = [[predicate_id]]
                target["corroborator_groups"] = []
                result = MODULE.resolve(value, subject, [self.candidate_instance(predicate_id, subject)])
                self.assertEqual(result["cause"], "unknown")
                with self.assertRaisesRegex(ValueError, "candidate-only predicate used as selector"):
                    MODULE.validate_mapping(value, self.contract)

    def test_same_operation_current_approval_resolves_but_cross_operation_does_not(self):
        case = next(item for item in self.mapping["proof_cases"] if item["case_id"] == "approval-same-operation-current")
        subject, instances = MODULE.proof_instances(case)
        self.assertEqual(MODULE.resolve(self.mapping, subject, instances)["cause"], "approval_propagating")
        instances[0]["subject"]["operation_id"] = "another-operation"
        instances[1]["subject"]["operation_id"] = "another-operation"
        self.assertEqual(MODULE.resolve(self.mapping, subject, instances)["cause"], "unknown")

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


if __name__ == "__main__":
    unittest.main()
