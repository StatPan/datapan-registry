from __future__ import annotations

import importlib.util
import pathlib
import unittest


ROOT = pathlib.Path(__file__).parents[1]


def load_script(name: str, path: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


GENERATOR = load_script("consumer_compatibility", "scripts/generate-release-consumer-compatibility.py")
VALIDATOR = load_script("validate_consumer_compatibility", "scripts/validate-release-consumer-compatibility.py")


def proof(ready: bool) -> dict:
    return {
        "summary": {
            "shard_preferred_ready": ready,
            "monolith_fallback_proven": ready,
            "distribution_action_resolved": ready,
        },
        "release_policy": {
            "consumer_effect": (
                "shard_preferred_supported_with_canonical_fallback"
                if ready
                else "canonical_registry_required_shards_optional"
            )
        },
    }


class StudioConsumerCompatibilityTest(unittest.TestCase):
    def test_proven_fallback_recovers_studio(self) -> None:
        shard_proof = proof(True)
        consumers = GENERATOR.consumer_entries(shard_proof)
        studio = next(row for row in consumers if row["consumer"] == "studio")
        self.assertEqual(studio["status"], "proven")
        self.assertIn("StatPan/datapan-cli#129", studio["evidence"])
        VALIDATOR.validate_consumers(
            {"consumers": consumers, "shard_consumer_proof": {"distribution_action_resolved": True, "canonical_registry_required": True}}
        )

    def test_unproven_fallback_keeps_studio_blocked(self) -> None:
        consumers = GENERATOR.consumer_entries(proof(False))
        studio = next(row for row in consumers if row["consumer"] == "studio")
        self.assertEqual(studio["status"], "blocked")
        VALIDATOR.validate_consumers(
            {"consumers": consumers, "shard_consumer_proof": {"distribution_action_resolved": False, "canonical_registry_required": True}}
        )


if __name__ == "__main__":
    unittest.main()
