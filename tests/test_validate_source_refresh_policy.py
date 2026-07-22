from __future__ import annotations

import pathlib
import subprocess
import sys
import tempfile
import unittest


ROOT = pathlib.Path(__file__).parents[1]
SCRIPT = ROOT / "scripts" / "validate-source-refresh-policy.py"
POLICY = ROOT / "policy" / "source-refresh.json"
SCHEMA = ROOT / "schemas" / "datapan.source-refresh-policy.v1.schema.json"
COVERAGE = ROOT / "policy" / "sustainable-coverage.json"
WORKFLOW = ROOT / ".github" / "workflows" / "upstream-catalog-refresh.yml"
CANONICAL_SECRET_MAPPING = "DATA_GO_KR_SERVICE_KEY: ${{ secrets.DATAPAN_DATA_GO_KR_SERVICE_KEY }}"


class ValidateSourceRefreshPolicyTest(unittest.TestCase):
    def run_validator(self, workflow: pathlib.Path) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [
                sys.executable,
                str(SCRIPT),
                "--policy", str(POLICY),
                "--schema", str(SCHEMA),
                "--coverage-policy", str(COVERAGE),
                "--workflow", str(workflow),
            ],
            cwd=ROOT,
            text=True,
            capture_output=True,
            check=False,
        )

    def test_accepts_canonical_data_go_kr_secret_mapping(self) -> None:
        result = self.run_validator(WORKFLOW)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_rejects_legacy_secret_mapping(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workflow = pathlib.Path(directory) / "upstream-catalog-refresh.yml"
            workflow.write_text(
                WORKFLOW.read_text(encoding="utf-8").replace(
                    CANONICAL_SECRET_MAPPING,
                    "DATA_GO_KR_SERVICE_KEY: ${{ secrets.DATA_GO_KR_SERVICE_KEY }}",
                ),
                encoding="utf-8",
            )
            result = self.run_validator(workflow)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("DATAPAN_DATA_GO_KR_SERVICE_KEY", result.stderr)


if __name__ == "__main__":
    unittest.main()
