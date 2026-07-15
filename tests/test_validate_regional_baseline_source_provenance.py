from __future__ import annotations

import copy
import importlib.util
import pathlib
import unittest
from unittest import mock

SCRIPT = pathlib.Path("scripts/validate-regional-baseline-source-provenance.py")
SPEC = importlib.util.spec_from_file_location("regional_provenance_validator", SCRIPT)
assert SPEC and SPEC.loader
validator = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(validator)


class RegionalBaselineSourceProvenanceTest(unittest.TestCase):
    def test_checked_in_contract_is_valid(self) -> None:
        validator.validate()

    def assert_artifact_rejected(self, mutate) -> None:
        original_load = validator.load
        artifact = copy.deepcopy(original_load(validator.ARTIFACT))
        mutate(artifact)

        def fake_load(path):
            return artifact if path == validator.ARTIFACT else original_load(path)

        with mock.patch.object(validator, "load", side_effect=fake_load):
            with self.assertRaises(Exception):
                validator.validate()

    def test_table_identity_drift_is_rejected(self) -> None:
        self.assert_artifact_rejected(lambda artifact: artifact["inputs"][0].update(table_id="DT_OTHER"))

    def test_blanket_rights_claim_is_rejected(self) -> None:
        self.assert_artifact_rejected(lambda artifact: artifact["rights"].update(assessment="unconditional_reuse"))

    def test_asserted_freshness_is_rejected(self) -> None:
        self.assert_artifact_rejected(lambda artifact: artifact["freshness"].update(mode="current"))

    def test_openapi_entitlement_claim_is_rejected(self) -> None:
        self.assert_artifact_rejected(lambda artifact: artifact["rights"].update(credential_scope="openapi_entitled"))

    def test_paid_redistribution_prohibition_removal_is_rejected(self) -> None:
        self.assert_artifact_rejected(lambda artifact: artifact["rights"].update(prohibited_uses=[]))

    def test_forbidden_product_ownership_fields_are_rejected(self) -> None:
        for key in ("current_pointer", "data_artifact_locator", "health_observations", "dataset_api"):
            with self.subTest(key=key):
                self.assert_artifact_rejected(lambda artifact, key=key: artifact.update({key: "forbidden"}))

    def test_manifest_digest_drift_is_rejected(self) -> None:
        original_load = validator.load
        manifest = copy.deepcopy(original_load(validator.MANIFEST))
        next(item for item in manifest["artifacts"] if item["path"] == validator.ARTIFACT.as_posix())["sha256"] = "0" * 64
        with mock.patch.object(validator, "load", side_effect=lambda path: manifest if path == validator.MANIFEST else original_load(path)):
            with self.assertRaisesRegex(ValueError, "digest drift"):
                validator.validate()

    def test_consumer_pin_drift_is_rejected(self) -> None:
        original_load = validator.load
        fixture = copy.deepcopy(original_load(validator.FIXTURE))
        fixture["registry_manifest"]["sha256"] = "0" * 64
        with mock.patch.object(validator, "load", side_effect=lambda path: fixture if path == validator.FIXTURE else original_load(path)):
            with self.assertRaisesRegex(ValueError, "manifest digest drift"):
                validator.validate()


if __name__ == "__main__":
    unittest.main()
