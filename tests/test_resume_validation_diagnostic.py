from __future__ import annotations

import importlib.util
import json
import pathlib
import unittest
from unittest import mock

import jsonschema


ROOT = pathlib.Path(__file__).parents[1]
SCRIPT = ROOT / "scripts" / "process-upstream-catalogue-candidate.py"
SPEC = importlib.util.spec_from_file_location("resume_validation_diagnostic_processor", SCRIPT)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)
CHECKPOINT_SCHEMA = json.loads(
    (ROOT / "schemas/datapan.upstream-catalogue-checkpoint.v1.schema.json").read_text(encoding="utf-8"),
)
DIAGNOSTIC_SCHEMA = CHECKPOINT_SCHEMA["$defs"]["resume_validation_diagnostic"]


class ResumeValidationDiagnosticTest(unittest.TestCase):
    def test_merge_conflict_is_a_typed_valueerror_with_a_phase_bound_code(self) -> None:
        resume = {"2": {"source_sha256": "a"}}
        canonical = {"2": {"source_sha256": "b"}}
        with self.assertRaises(MODULE.ResumeCacheValidationError) as caught:
            MODULE.merge_derivation_enrichment_records(resume, canonical)

        error = caught.exception
        self.assertIs(type(error), MODULE.ResumeCacheValidationError)
        self.assertIsInstance(error, ValueError)
        self.assertEqual(str(error), "derivation_parent_enrichment_contribution_conflict")
        with mock.patch.object(
            MODULE.ResumeCacheValidationError,
            "__str__",
            side_effect=AssertionError("typed classification must precede exception formatting"),
        ):
            self.assertEqual(MODULE.safe_error_class(error), "valueerror")
        diagnostic = MODULE.resume_validation_diagnostic(error, "parent_cache_merge")
        self.assertEqual(diagnostic, {
            "version": 1,
            "phase": "parent_cache_merge",
            "code": "derivation_parent_enrichment_contribution_conflict",
        })
        self.assertIsNone(MODULE.resume_validation_diagnostic(error, "resume_parent_cache"))
        jsonschema.Draft202012Validator(DIAGNOSTIC_SCHEMA).validate(diagnostic)

    def test_untrusted_error_text_and_duck_typed_code_use_only_safe_fallback(self) -> None:
        class FakeCacheError(ValueError):
            code = "resume_artifact_unbound"

            def __str__(self) -> str:
                return "SECRET path or token"

        forged = FakeCacheError()
        diagnostic = MODULE.resume_validation_diagnostic(forged, "resume_parent_cache")
        self.assertEqual(diagnostic, {
            "version": 1,
            "phase": "resume_parent_cache",
            "code": "validation_error",
        })
        self.assertNotIn("SECRET", json.dumps(diagnostic))
        self.assertNotIn(forged.code, json.dumps(diagnostic))
        self.assertEqual(MODULE.safe_error_class(forged), "fakecacheerror")

        with self.assertRaisesRegex(ValueError, "invalid_resume_validation_error_code"):
            MODULE.ResumeCacheValidationError("SECRET unregistered code")
        error = MODULE.ResumeCacheValidationError("resume_artifact_unbound")
        self.assertEqual(MODULE.safe_error_class(error), "valueerror")
        self.assertNotIn("resume_artifact_unbound", MODULE.safe_error_class(error))

        fallback_cases = (
            (json.JSONDecodeError("SECRET malformed input", "", 0), "json_decode_error"),
            (OSError("SECRET /private/path"), "io_error"),
            (ValueError("SECRET free text"), "validation_error"),
        )
        for exc, code in fallback_cases:
            with self.subTest(code=code):
                safe = MODULE.resume_validation_diagnostic(exc, "canonical_parent_cache")
                self.assertEqual(safe, {
                    "version": 1,
                    "phase": "canonical_parent_cache",
                    "code": code,
                })
                self.assertNotIn("SECRET", json.dumps(safe))

    def test_schema_is_optional_at_outcome_and_closed_for_diagnostics(self) -> None:
        outcome_schema = CHECKPOINT_SCHEMA["properties"]["outcome"]
        self.assertIn("resume_validation_diagnostic", outcome_schema["properties"])
        self.assertNotIn("required", outcome_schema)

        valid = {"version": 1, "phase": "resume_parent_cache", "code": "resume_artifact_unbound"}
        jsonschema.Draft202012Validator(DIAGNOSTIC_SCHEMA).validate(valid)
        jsonschema.Draft202012Validator(DIAGNOSTIC_SCHEMA).validate({
            "version": 1, "phase": "parent_cache_merge", "code": "io_error",
        })

        invalid_values = (
            {"version": 1, "phase": "resume_parent_cache", "code": "secret-path"},
            {"version": 1, "phase": "parent_cache_merge", "code": "resume_artifact_unbound"},
            {**valid, "message": "SECRET"},
            {"version": 2, "phase": "resume_parent_cache", "code": "validation_error"},
        )
        validator = jsonschema.Draft202012Validator(DIAGNOSTIC_SCHEMA)
        for value in invalid_values:
            with self.subTest(value=value), self.assertRaises(jsonschema.ValidationError):
                validator.validate(value)


if __name__ == "__main__":
    unittest.main()
