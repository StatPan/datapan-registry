import copy
import importlib.util
import pathlib
import subprocess
import sys
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "diagnostic_mapping", ROOT / "scripts/validate-diagnostic-evidence-mapping-draft.py"
)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC.loader
SPEC.loader.exec_module(MODULE)


class DiagnosticEvidenceMappingDraftTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.mapping = MODULE.load(MODULE.MAPPING)
        cls.contract = MODULE.load(MODULE.CONTRACT)

    def test_checked_in_mapping_and_packets(self):
        self.assertEqual(
            MODULE.validate_all(),
            {"inputs": 5, "causes": 11, "proof_cases": 5, "consumers": 3},
        )

    def test_cli_entrypoint(self):
        result = subprocess.run(
            [sys.executable, str(ROOT / "scripts/validate-diagnostic-evidence-mapping-draft.py")],
            check=False, capture_output=True, text=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("consumers=3", result.stdout)

    def test_code30_and_http401_cannot_select_specific_cause(self):
        for signal in (
            "registry_rule:data-go-kr-service-key-not-registered",
            "provider_response:http-401",
        ):
            with self.subTest(signal=signal):
                self.assertEqual(MODULE.eligible_causes(self.mapping, {signal}), [])

    def test_conflicting_specific_causes_fall_back_to_unknown(self):
        signals = {
            "provider_response:class=credential_rejected,policy_version=present",
            "provider_response:class=rate_limited,policy_version=present",
        }
        eligible = MODULE.eligible_causes(self.mapping, signals)
        self.assertEqual(eligible, ["credential_invalid", "rate_limited"])
        self.assertEqual("unknown" if len(eligible) != 1 else eligible[0], "unknown")

    def test_rejects_action_drift(self):
        value = copy.deepcopy(self.mapping)
        value["cause_mappings"][0]["action"] = "reissue_credential"
        with self.assertRaisesRegex(ValueError, "action drift"):
            MODULE.validate_mapping(value, self.contract)

    def test_rejects_accountable_party_drift(self):
        value = copy.deepcopy(self.mapping)
        value["cause_mappings"][0]["accountable_party"] = "provider"
        with self.assertRaisesRegex(ValueError, "accountable party drift"):
            MODULE.validate_mapping(value, self.contract)

    def test_rejects_live_registry_inference(self):
        value = copy.deepcopy(self.mapping)
        value["authority_boundary"]["runtime_inference_owner"] = "registry"
        with self.assertRaisesRegex(ValueError, "live inference"):
            MODULE.validate_mapping(value, self.contract)

    def test_rejects_generic_signal_upgrade(self):
        value = copy.deepcopy(self.mapping)
        value["candidate_only_signals"].remove("provider_response:http-401")
        with self.assertRaisesRegex(ValueError, "candidate-only"):
            MODULE.validate_mapping(value, self.contract)

    def test_rejects_missing_avoid_action(self):
        value = copy.deepcopy(self.mapping)
        item = next(item for item in value["cause_mappings"] if item["cause"] == "approval_propagating")
        item["avoid"] = []
        with self.assertRaisesRegex(ValueError, "missing required avoid"):
            MODULE.validate_mapping(value, self.contract)

    def test_rejects_mapping_token_outside_envelope_vocabulary(self):
        value = copy.deepcopy(self.mapping)
        item = next(item for item in value["cause_mappings"] if item["cause"] == "semantic_quality")
        item["required_evidence_groups"][0][0] = "data_quality_assertion:kind=semantic,result=failed,policy_version=present"
        with self.assertRaisesRegex(ValueError, "mapping vocabulary drift"):
            MODULE.validate_mapping_vocabulary(value)

    def test_outage_variants_distinguish_credential_independent_evidence(self):
        item = next(item for item in self.mapping["cause_mappings"] if item["cause"] == "provider_outage")
        correlated, direct_response = item["action_variants"]
        self.assertEqual(correlated["avoid"], ["reissue_credential"])
        self.assertEqual(direct_response["avoid"], [])

    def test_consumer_packets_do_not_claim_dependency_gated_proof(self):
        for path in MODULE.PACKETS.glob("*.v1.json"):
            packet = MODULE.load(path)
            self.assertEqual(packet["production_status"]["currently_proven"], [])
            self.assertTrue(packet["production_status"]["required_after_dependencies"])

    def test_authoritative_input_digest_is_pinned(self):
        value = copy.deepcopy(self.mapping)
        value["authoritative_inputs"][0]["sha256"] = "0" * 64
        with self.assertRaisesRegex(ValueError, "digest drift"):
            MODULE.validate_inputs(value)


if __name__ == "__main__":
    unittest.main()
