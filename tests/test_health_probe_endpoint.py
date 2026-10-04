import copy
import importlib.util
import pathlib
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("health_endpoint", ROOT / "scripts/health_probe_endpoint.py")
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class HealthEndpointTest(unittest.TestCase):
    def inputs(self, endpoint="http://www.korad.or.kr/openapi/service/weatherMoniterSvc"):
        return (
            {"operation_id": "dpr-op-00000007", "endpoint_correction": copy.deepcopy(MODULE.KORAD_CORRECTION)},
            {"id": "15004206"},
            {"endpoint": endpoint, "source": {"raw": {"operation_seq": "6836"}}},
        )

    def test_documented_method_preserves_source_provenance_and_protocol(self):
        result = MODULE.resolve_endpoint(*self.inputs())
        self.assertEqual(result, {"scheme": "http", "host": "www.korad.or.kr", "path": MODULE.KORAD_METHOD, "source_path": MODULE.KORAD_ROOT, "correction_source": MODULE.KORAD_SOURCE})

    def test_corrected_upstream_method_keeps_same_request_identity(self):
        result = MODULE.resolve_endpoint(*self.inputs("https://www.korad.or.kr" + MODULE.KORAD_METHOD))
        self.assertEqual(result["path"], MODULE.KORAD_METHOD)
        self.assertEqual(result["source_path"], MODULE.KORAD_METHOD)
        self.assertEqual(result["scheme"], "https")

    def test_source_query_values_are_never_projected(self):
        result = MODULE.resolve_endpoint(*self.inputs("http://www.korad.or.kr" + MODULE.KORAD_ROOT + "?type=json&serviceKey=secret-marker"))
        self.assertNotIn("secret-marker", str(result))
        self.assertNotIn("?", result["path"])

    def test_missing_method_correction_fails_closed(self):
        selection, dataset, operation = self.inputs()
        selection.pop("endpoint_correction")
        with self.assertRaisesRegex(ValueError, "requires its reviewed operation correction"):
            MODULE.resolve_endpoint(selection, dataset, operation)

    def test_source_and_policy_drift_cannot_reuse_correction(self):
        for field in ("dataset", "sequence", "source_path", "policy_path", "policy_document"):
            with self.subTest(field=field):
                selection, dataset, operation = self.inputs()
                if field == "dataset": dataset["id"] = "other"
                if field == "sequence": operation["source"]["raw"]["operation_seq"] = "other"
                if field == "source_path": operation["endpoint"] += "/other"
                if field == "policy_path": selection["endpoint_correction"]["request_path"] += "/other"
                if field == "policy_document": selection["endpoint_correction"]["source"] = "https://example.test"
                with self.assertRaises(ValueError): MODULE.resolve_endpoint(selection, dataset, operation)

    def test_malformed_transport_rejected(self):
        for endpoint in ("ftp://www.korad.or.kr/x", "http://user@www.korad.or.kr/x", "http://www.korad.or.kr:80/x", "http://www.korad.or.kr//x", "http://www.korad.or.kr/x#fragment", "http://www.korad.or.kr/x\n"):
            with self.subTest(endpoint=endpoint):
                with self.assertRaises(ValueError): MODULE.resolve_endpoint(*self.inputs(endpoint))
