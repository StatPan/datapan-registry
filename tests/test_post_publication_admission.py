from __future__ import annotations

import copy
import json
import pathlib
import subprocess
import sys
import tempfile
import unittest


ROOT = pathlib.Path(__file__).parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import post_publication_admission as admission  # noqa: E402


class PostPublicationAdmissionTest(unittest.TestCase):
    def setUp(self) -> None:
        self.schema = admission.load_json(ROOT / "schemas/datapan.post-publication-admission.v1.schema.json")
        self.fixture_path = ROOT / "tests/fixtures/post-publication-admission/accepted.json"
        self.evidence_root = ROOT / "tests/fixtures/post-publication-admission/evidence"
        self.value = admission.load_json(self.fixture_path)
        self.seal(self.value)

    @staticmethod
    def seal(value: dict) -> None:
        value["admission_digest"] = admission.canonical_digest(value)

    def validate(self, value: dict, *, when: str = "2026-08-05T00:10:00Z", evidence_root: pathlib.Path | None = None) -> str:
        return admission.validate_admission(value, schema=self.schema, admitted_at=admission.parse_time(when, "test admission time"), evidence_root=evidence_root)

    def test_checked_in_fixture_passes_the_documented_offline_command(self) -> None:
        result = subprocess.run(
            [sys.executable, "scripts/validate-post-publication-admission.py", "--admission-time", "2026-08-05T00:10:00Z", str(self.fixture_path)],
            cwd=ROOT,
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("status=accepted", result.stdout)

    def test_accepted_receipt_requires_anonymous_then_exact_cli_binding_within_600_seconds(self) -> None:
        self.assertEqual(self.validate(self.value), "accepted")
        for field, replacement in (("payload_revision", "d" * 40), ("pointer_sha256", "f" * 64), ("payload_manifest_sha256", "f" * 64), ("registry_revision", "d" * 40), ("registry_manifest_sha256", "f" * 64), ("registry_source_sha256", "f" * 64), ("registry_policy_sha256", "f" * 64)):
            with self.subTest(field=field):
                mutated = copy.deepcopy(self.value)
                mutated["cli_observation"]["binding"][field] = replacement
                self.seal(mutated)
                with self.assertRaisesRegex(ValueError, "binding"):
                    self.validate(mutated)

    def test_time_is_caller_owned_fresh_and_ordered(self) -> None:
        with self.assertRaisesRegex(ValueError, "stale"):
            self.validate(copy.deepcopy(self.value), when="2026-08-05T00:10:01Z")
        future = copy.deepcopy(self.value)
        future["cli_observation"]["observed_at"] = "2026-08-05T00:11:00Z"
        self.seal(future)
        with self.assertRaisesRegex(ValueError, "future"):
            self.validate(future)
        reordered = copy.deepcopy(self.value)
        reordered["cli_observation"]["observed_at"] = "2026-08-04T23:59:59Z"
        self.seal(reordered)
        with self.assertRaisesRegex(ValueError, "strictly later"):
            self.validate(reordered)
        equal = copy.deepcopy(self.value)
        equal["cli_observation"]["observed_at"] = equal["anonymous_verification"]["verified_at"]
        self.seal(equal)
        with self.assertRaisesRegex(ValueError, "strictly later"):
            self.validate(equal)

    def test_accepted_requires_all_three_cli_checks(self) -> None:
        mutated = copy.deepcopy(self.value)
        mutated["cli_observation"]["checks"] = ["install", "doctor"]
        self.seal(mutated)
        with self.assertRaisesRegex(ValueError, "exactly"):
            self.validate(mutated)

    def test_failed_cli_requires_observed_rollback_and_verified_recovery_or_manual_hold(self) -> None:
        rejected = copy.deepcopy(self.value)
        rejected["cli_observation"]["outcome"] = "failed"
        rejected["resolution"] = {"outcome": "rolled_back"}
        self.seal(rejected)
        with self.assertRaisesRegex(ValueError, "schema"):
            self.validate(rejected)

        rolled_back = copy.deepcopy(self.value)
        rolled_back["cli_observation"]["outcome"] = "failed"
        prior_receipt = admission.load_json(self.evidence_root / "prior-anonymous-verification.json")
        prior = prior_receipt["binding"]
        rollback = {
            "observed_at": "2026-08-05T00:06:00Z",
            "prior_anonymous_receipt": {"path": "prior-anonymous-verification.json", "sha256": admission.file_digest(self.evidence_root / "prior-anonymous-verification.json")},
            "receipt_sha256": "a" * 64,
        }
        recovery = copy.deepcopy(rolled_back["cli_observation"])
        recovery.update({"observed_at": "2026-08-05T00:07:00Z", "outcome": "verified", "binding": prior, "receipt_sha256": "b" * 64})
        rolled_back["resolution"] = {"outcome": "rolled_back", "rollback": rollback, "recovery_cli_observation": recovery}
        self.seal(rolled_back)
        self.assertEqual(self.validate(rolled_back, evidence_root=self.evidence_root), "rolled_back")
        with self.assertRaisesRegex(ValueError, "evidence root"):
            self.validate(rolled_back)

        non_rollback = copy.deepcopy(rolled_back)
        initial_binding = copy.deepcopy(self.value["anonymous_verification"]["binding"])
        non_rollback["resolution"]["recovery_cli_observation"]["binding"] = initial_binding
        self.seal(non_rollback)
        with self.assertRaisesRegex(ValueError, "binding"):
            self.validate(non_rollback, evidence_root=self.evidence_root)

        self_asserted = copy.deepcopy(rolled_back)
        self_asserted["resolution"]["rollback"].pop("prior_anonymous_receipt")
        self_asserted["resolution"]["rollback"]["prior_binding"] = prior
        self.seal(self_asserted)
        with self.assertRaisesRegex(ValueError, "schema"):
            self.validate(self_asserted, evidence_root=self.evidence_root)

        fabricated = copy.deepcopy(rolled_back)
        fabricated["resolution"]["rollback"]["prior_anonymous_receipt"]["sha256"] = "f" * 64
        self.seal(fabricated)
        with self.assertRaisesRegex(ValueError, "digest"):
            self.validate(fabricated, evidence_root=self.evidence_root)

        for path in (
            ("resolution", "rollback", "observed_at"),
            ("resolution", "recovery_cli_observation", "observed_at"),
        ):
            with self.subTest(path=path):
                equal = copy.deepcopy(rolled_back)
                if path[-1] == "observed_at" and path[-2] == "rollback":
                    equal["resolution"]["rollback"]["observed_at"] = equal["cli_observation"]["observed_at"]
                else:
                    equal["resolution"]["recovery_cli_observation"]["observed_at"] = prior_receipt["verified_at"]
                self.seal(equal)
                with self.assertRaisesRegex(ValueError, "strictly later"):
                    self.validate(equal, evidence_root=self.evidence_root)

        with tempfile.TemporaryDirectory() as temporary:
            root = pathlib.Path(temporary)
            prior_equal = copy.deepcopy(prior_receipt)
            prior_equal["verified_at"] = rolled_back["resolution"]["rollback"]["observed_at"]
            prior_path = root / "prior.json"
            prior_path.write_text(json.dumps(prior_equal), encoding="utf-8")
            equal = copy.deepcopy(rolled_back)
            equal["resolution"]["rollback"]["prior_anonymous_receipt"] = {"path": "prior.json", "sha256": admission.file_digest(prior_path)}
            equal["resolution"]["recovery_cli_observation"]["binding"] = prior_equal["binding"]
            self.seal(equal)
            with self.assertRaisesRegex(ValueError, "strictly later"):
                self.validate(equal, evidence_root=root)

        held = copy.deepcopy(self.value)
        held["cli_observation"]["outcome"] = "failed"
        held["resolution"] = {"outcome": "manual_hold", "hold_reason": "cli_observation_failed"}
        self.seal(held)
        self.assertEqual(self.validate(held), "manual_hold")

    def test_manual_hold_command_is_explicit_non_success(self) -> None:
        held = copy.deepcopy(self.value)
        held["cli_observation"]["outcome"] = "unknown"
        held["resolution"] = {"outcome": "manual_hold", "hold_reason": "cli_observation_incomplete"}
        self.seal(held)
        path = ROOT / ".datapan" / "post-publication-admission-manual-hold-test.json"
        path.parent.mkdir(exist_ok=True)
        path.write_text(json.dumps(held), encoding="utf-8")
        self.addCleanup(path.unlink)
        result = subprocess.run(
            [sys.executable, "scripts/validate-post-publication-admission.py", "--admission-time", "2026-08-05T00:10:00Z", str(path)],
            cwd=ROOT,
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertIn("status=manual_hold", result.stdout)

    def test_schema_and_diagnostics_do_not_admit_raw_endpoints_or_secrets(self) -> None:
        mutated = copy.deepcopy(self.value)
        mutated["cli_observation"]["request_url"] = "redacted-input"
        self.seal(mutated)
        with self.assertRaises(ValueError) as caught:
            self.validate(mutated)
        self.assertNotIn("redacted-input", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
