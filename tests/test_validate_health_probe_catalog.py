import copy
import importlib.util
import pathlib
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("health_catalog", ROOT / "scripts/validate-health-probe-catalog.py")
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC.loader
SPEC.loader.exec_module(MODULE)


class HealthProbeCatalogTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.catalog = MODULE.load(ROOT / "reports/health-probe-catalog.json")
        cls.schema = MODULE.load(ROOT / "schemas/datapan.health-probe-catalog.v1.schema.json")
        cls.registry = MODULE.load(ROOT / "data/data-go-kr.registry.json")
        cls.manifest = MODULE.load(ROOT / "manifest.json")
        cls.fixture = MODULE.load(ROOT / "fixtures/health-probe-catalog/cli-health-probe-v1.json")

    def validate(self, catalog=None, fixture=None):
        MODULE.validate_catalog(catalog or self.catalog, self.schema, self.registry, self.manifest, fixture or self.fixture)

    def test_checked_in_catalog(self):
        self.validate()

    def test_duplicate_operation_identity_rejected(self):
        value = copy.deepcopy(self.catalog)
        value["entries"][1]["operation_id"] = value["entries"][0]["operation_id"]
        with self.assertRaisesRegex(ValueError, "operation_id must be unique"):
            self.validate(value)

    def test_selector_drift_rejected(self):
        value = copy.deepcopy(self.fixture)
        value["cases"][0]["selector"]["operation"] = "renamed"
        with self.assertRaisesRegex(ValueError, "fixture selector drift"):
            self.validate(fixture=value)

    def test_auth_parameter_rejected(self):
        value = copy.deepcopy(self.catalog)
        value["entries"][0]["execution"]["safe_parameters"][0]["name"] = "ServiceKey"
        with self.assertRaisesRegex(ValueError, "unsafe or unknown parameter"):
            self.validate(value)

    def test_underspecified_freshness_rejected(self):
        value = copy.deepcopy(self.catalog)
        value["entries"][0]["response_freshness"] = {"mode": "asserted"}
        with self.assertRaises(Exception):
            self.validate(value)

    def test_unsafe_status_cannot_retain_execution_policy(self):
        value = copy.deepcopy(self.catalog)
        value["entries"][0]["eligibility"] = {"status": "unsupported", "reason_code": "unsafe_parameters"}
        with self.assertRaises(Exception):
            self.validate(value)

    def test_mutable_receipt_rejected(self):
        value = copy.deepcopy(self.catalog)
        value["entries"][0]["receipt"] = {}
        with self.assertRaises(Exception):
            self.validate(value)


if __name__ == "__main__":
    unittest.main()
