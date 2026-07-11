from __future__ import annotations

import importlib.util
import pathlib
import unittest


SCRIPT = pathlib.Path(__file__).parents[1] / "scripts" / "generate-runtime-freshness-batch.py"
SPEC = importlib.util.spec_from_file_location("generate_runtime_freshness_batch", SCRIPT)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def queue(count: int) -> dict:
    return {"queue": [{"source_id": "data_go_kr", "identity_key": f"data_go_kr:d:{i}", "dataset_id": "d", "operation": f"o{i}", "operation_seq": str(i), "classification": "never_evidenced", "priority": 2} for i in range(count)]}


class RuntimeFreshnessBatchTest(unittest.TestCase):
    def test_selection_rotates_by_seed_and_shard(self) -> None:
        first, meta = MODULE.select(queue(20), rotation_seed=0, shard_index=1, shard_count=2, batch_size=3)
        second, _ = MODULE.select(queue(20), rotation_seed=1, shard_index=1, shard_count=2, batch_size=3)
        self.assertEqual(meta["offset"], 3)
        self.assertEqual([row["operation_seq"] for row in first], ["3", "4", "5"])
        self.assertEqual([row["operation_seq"] for row in second], ["9", "10", "11"])

    def test_selection_wraps_without_duplicates(self) -> None:
        selected, meta = MODULE.select(queue(7), rotation_seed=1, shard_index=1, shard_count=2, batch_size=4)
        self.assertTrue(meta["wrapped"])
        self.assertEqual([row["operation_seq"] for row in selected], ["5", "6", "0", "1"])
        self.assertEqual(len({row["identity_key"] for row in selected}), 4)

    def test_materialized_registry_exactly_matches_selection(self) -> None:
        selected = queue(2)["queue"]
        registry = [{"id": "d", "operations": [{"name": "o0", "source": {"raw": {"operation_seq": "0"}}}, {"name": "o1", "source": {"raw": {"operation_seq": "1"}}}, {"name": "other", "source": {"raw": {"operation_seq": "2"}}}]}]
        result = MODULE.materialize(registry, selected)
        self.assertEqual([row["name"] for row in result[0]["operations"]], ["o0", "o1"])


if __name__ == "__main__":
    unittest.main()
