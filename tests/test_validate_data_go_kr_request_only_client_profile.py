from __future__ import annotations

import copy
import importlib.util
import pathlib
import subprocess
import sys
import unittest


ROOT = pathlib.Path(__file__).parents[1]
SPEC = importlib.util.spec_from_file_location("request_only_profile", ROOT / "scripts/validate-data-go-kr-request-only-client-profile.py")
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
SPEC.loader.exec_module(MODULE)


class RequestOnlyClientProfileTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.profile = MODULE.load(ROOT / "reports/data-go-kr/request-only-client-profile.json")
        cls.schema = MODULE.load(ROOT / "schemas/datapan.request-only-client-profile.v1.schema.json")
        cls.source = MODULE.load(ROOT / "reports/data-go-kr/operation-manifest.json")
        cls.manifest = MODULE.load(ROOT / "manifest.json")
        cls.fixture = MODULE.load(ROOT / "fixtures/request-only-client-profile/registry-local-consumer-proof.v1.json")

    def validate(self, profile=None, fixture=None):
        MODULE.validate(profile or self.profile, self.schema, self.source, self.manifest, fixture or self.fixture)

    def test_checked_in_profile(self):
        self.validate()

    def test_generation_is_deterministic(self):
        command = [sys.executable, str(ROOT / "scripts/generate-data-go-kr-request-only-client-profile.py"), "--check"]
        first = subprocess.run(command, capture_output=True, text=True, check=False)
        second = subprocess.run(command, capture_output=True, text=True, check=False)
        self.assertEqual((first.returncode, first.stdout, first.stderr), (0, second.stdout, second.stderr))

    def test_duplicate_identity_rejected(self):
        value = copy.deepcopy(self.profile)
        value["operations"][1]["operation_id"] = value["operations"][0]["operation_id"]
        with self.assertRaisesRegex(ValueError, "duplicate operation identity"):
            self.validate(value)

    def test_unsupported_outcome_omission_rejected(self):
        value = copy.deepcopy(self.profile)
        value["operations"][0].pop("outcome")
        with self.assertRaises(Exception):
            self.validate(value)

    def test_executable_claim_rejected(self):
        value = copy.deepcopy(self.profile)
        value["capabilities"]["typed_response"] = True
        with self.assertRaises(Exception):
            self.validate(value)

    def test_consumer_profile_pin_drift_rejected(self):
        fixture = copy.deepcopy(self.fixture)
        fixture["pins"]["profile"]["sha256"] = "0" * 64
        with self.assertRaisesRegex(ValueError, "consumer profile pin mismatch"):
            self.validate(fixture=fixture)


if __name__ == "__main__":
    unittest.main()
