from __future__ import annotations

import importlib.util
import pathlib
import unittest


SCRIPT = pathlib.Path(__file__).parents[1] / "scripts" / "generate-sustainable-coverage.py"
SPEC = importlib.util.spec_from_file_location("generate_sustainable_coverage", SCRIPT)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class SustainableCoverageTest(unittest.TestCase):
    def load_inputs(self):
        policy = MODULE.load_json(MODULE.POLICY_PATH)
        inputs = {name: MODULE.load_json(path) for name, path in MODULE.INPUT_PATHS.items()}
        return policy, inputs

    def test_latest_verification_time_can_advance_beyond_release_manifest(self) -> None:
        policy, inputs = self.load_inputs()
        evaluation_time = "2026-07-24T00:00:01Z"
        self.assertLess(inputs["manifest"]["generated_at"], evaluation_time)
        inputs["latest_verification"]["generated_at"] = evaluation_time
        inputs["runtime_freshness_queue"]["generated_at"] = evaluation_time
        inputs["runtime_freshness_queue"]["freshness"]["as_of"] = evaluation_time

        report = MODULE.build_report(policy, inputs)

        self.assertEqual(report["generated_at"], evaluation_time)
        self.assertEqual(report["freshness"]["as_of"], evaluation_time)

    def test_queue_and_verification_times_must_match(self) -> None:
        policy, inputs = self.load_inputs()
        inputs["latest_verification"]["generated_at"] = "2026-07-24T00:00:01Z"

        with self.assertRaisesRegex(ValueError, "evaluation times differ"):
            MODULE.build_report(policy, inputs)


if __name__ == "__main__":
    unittest.main()
