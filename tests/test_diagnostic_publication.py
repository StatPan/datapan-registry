import copy
import importlib.util
import json
import pathlib
import subprocess
import sys
import unittest

import jsonschema


ROOT = pathlib.Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "diagnostic_publication", ROOT / "scripts/generate-diagnostic-publication.py"
)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC.loader
SPEC.loader.exec_module(MODULE)


class DiagnosticPublicationTest(unittest.TestCase):
    def test_checked_in_publication_is_deterministic_and_release_bound(self):
        outputs = MODULE.build()
        self.assertEqual(MODULE.check(outputs), [])
        MODULE.validate_release_bindings(outputs)
        self.assertEqual(len(outputs), 19)

    def test_entrypoint_check(self):
        result = subprocess.run(
            [sys.executable, "scripts/generate-diagnostic-publication.py", "--check"],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(MODULE.CANDIDATE_BINDING, result.stdout)

    def test_public_examples_validate_against_stable_schema(self):
        schema = json.loads((ROOT / MODULE.SCHEMA_PUBLIC).read_text(encoding="utf-8"))
        validator = jsonschema.Draft202012Validator(schema)
        self.assertNotIn("Draft", schema["title"])
        for path in MODULE.FIXTURE_PUBLIC.values():
            validator.validate(json.loads((ROOT / path).read_text(encoding="utf-8")))

    def test_public_contracts_have_stable_status_and_no_draft_paths(self):
        outputs = MODULE.build()
        for path in [
            MODULE.CONTRACT_PUBLIC,
            MODULE.MAPPING_PUBLIC,
            *MODULE.COMPATIBILITY_PUBLIC.values(),
        ]:
            value = json.loads(outputs[path])
            self.assertEqual(value["status"], "stable")
            self.assertNotIn("drafts/diagnostic-envelope/", outputs[path].decode("utf-8"))

    def test_candidate_identity_cannot_be_reanchored(self):
        candidate = copy.deepcopy(MODULE.load(MODULE.CANDIDATE))
        candidate["binding_sha256"] = "0" * 64
        with self.assertRaisesRegex(ValueError, "accepted candidate"):
            MODULE.validate_candidate(candidate)

    def test_sensitive_release_material_fails_closed(self):
        forbidden_values = [
            {"authorization": "Bearer redacted"},
            {"credential_hash": "0" * 64},
            {"nested": {"raw_response_body": "{}"}},
            {"request_headers": {}},
            {"url": "https://example.test/path?serviceKey=redacted"},
            {"note": "api_key=redacted"},
            {"user_telemetry": []},
        ]
        for value in forbidden_values:
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    MODULE.inspect_safe(value)

    def test_readiness_never_claims_publication_or_runtime_authority(self):
        readiness = json.loads((ROOT / MODULE.READINESS_PUBLIC).read_text(encoding="utf-8"))
        readiness_schema = json.loads(
            (ROOT / "schemas/datapan.diagnostic-publication-readiness.v1.schema.json").read_text(
                encoding="utf-8"
            )
        )
        jsonschema.Draft202012Validator(
            readiness_schema, format_checker=jsonschema.FormatChecker()
        ).validate(readiness)
        self.assertEqual(readiness["status"], "prepared_for_exact_head_review")
        self.assertTrue(all(value is False for value in readiness["authority"].values()))
        self.assertEqual(readiness["gates"]["anonymous_immutable_fetch"], "blocked_until_publication")
        self.assertEqual(
            readiness["gates"]["datapan_web_adoption"],
            "blocked_until_anonymous_immutable_fetch",
        )


if __name__ == "__main__":
    unittest.main()
