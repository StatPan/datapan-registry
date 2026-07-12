import json
import pathlib
import subprocess
import sys
import tempfile
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "validate-source-runtime-candidates.py"


class SourceRuntimeCandidateValidationTest(unittest.TestCase):
    def write_json(self, path: pathlib.Path, value: object) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")

    def test_rejects_credential_placeholder_drift(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = pathlib.Path(temp)
            profile = root / "sources" / "sample_source.json"
            batch = root / "reports" / "sample-source" / "runtime-candidates.json"
            self.write_json(
                profile,
                {
                    "schema_version": "datapan.source-profile.v1",
                    "source_id": "sample_source",
                    "provider": "example.test",
                    "references": {
                        "homepage_url": "https://example.test",
                        "last_reviewed_at": "2026-07-12",
                    },
                    "auth": {
                        "type": "api_key",
                        "key_parameter_names": ["KEY"],
                        "key_locations": ["query"],
                    },
                    "request": {"methods": ["GET"]},
                    "response": {"formats": ["json"]},
                    "runtime": {"sample_param_policy": "static"},
                },
            )
            self.write_json(
                batch,
                {
                    "schema_version": "datapan.source-runtime-candidates.v1",
                    "generated_at": "2026-07-12T00:00:00Z",
                    "provider": "example.test",
                    "source_id": "sample_source",
                    "source_profile": profile.as_posix(),
                    "references": {
                        "official_reference_urls": ["https://example.test"],
                        "last_reviewed_at": "2026-07-12",
                    },
                    "summary": {
                        "candidates": 1,
                        "pinned_sample_count": 1,
                        "credential_required": True,
                        "evidence_total": 0,
                    },
                    "candidates": [
                        {
                            "candidate_id": "sample-candidate",
                            "label": "Sample candidate",
                            "operation_kind": "catalogue_list",
                            "method": "GET",
                            "endpoint_template": "https://example.test/items",
                            "format": "json",
                            "sample_parameters": {"KEY": "${WRONG_KEY}", "pSize": "1"},
                            "credential_policy": {
                                "required": True,
                                "key_names": ["KEY"],
                                "injection_location": "query",
                                "placeholder": "${SAMPLE_KEY}",
                            },
                            "evidence_status": "not_collected",
                            "promotion_status": "registry_only",
                            "reference_url": "https://example.test",
                        }
                    ],
                },
            )

            result = subprocess.run(
                [
                    sys.executable,
                    SCRIPT.as_posix(),
                    "--schema",
                    (ROOT / "schemas" / "datapan.source-runtime-candidates.v1.schema.json").as_posix(),
                    batch.as_posix(),
                ],
                cwd=ROOT,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("credential placeholder drift", result.stderr)


if __name__ == "__main__":
    unittest.main()
