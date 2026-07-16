import importlib.util
import json
import pathlib
import tempfile
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "validate_external_checkout_refs", ROOT / "scripts/validate-external-checkout-refs.py"
)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(MODULE)


class ExternalCheckoutRefContractTests(unittest.TestCase):
    SHA = "a" * 40

    def contract(self, root: pathlib.Path, **overrides):
        value = {
            "schema_version": "datapan.external-checkout-refs.v1",
            "repository": "StatPan/datapan-cli",
            "ref_kind": "commit_sha",
            "ref": self.SHA,
            "update_policy": {"mutable_refs_allowed": False},
        }
        value.update(overrides)
        path = root / "contract.json"
        path.write_text(json.dumps(value), encoding="utf-8")
        return path

    def workflow(self, root: pathlib.Path, ref_line: str):
        directory = root / "workflows"
        directory.mkdir()
        (directory / "verify.yml").write_text(
            "jobs:\n"
            "  verify:\n"
            "    steps:\n"
            "      - name: Checkout CLI\n"
            "        uses: actions/checkout@v7\n"
            "        with:\n"
            "          repository: StatPan/datapan-cli\n"
            f"{ref_line}"
            "          path: datapan-cli\n",
            encoding="utf-8",
        )
        return directory

    def test_inventory_records_exact_reviewed_identity(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = pathlib.Path(temporary)
            report = MODULE.build_report(
                self.contract(root), self.workflow(root, f"          ref: {self.SHA}\n")
            )
            self.assertEqual(report["selected_ref"], self.SHA)
            self.assertEqual(report["checkout_count"], 1)
            self.assertEqual(report["workflows"][0]["checkouts"][0]["ref"], self.SHA)

    def test_missing_ref_is_rejected_before_default_branch_lookup(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = pathlib.Path(temporary)
            with self.assertRaisesRegex(MODULE.ContractError, "missing ref"):
                MODULE.build_report(self.contract(root), self.workflow(root, ""))

    def test_ref_drift_is_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = pathlib.Path(temporary)
            with self.assertRaisesRegex(MODULE.ContractError, "does not match reviewed identity"):
                MODULE.build_report(self.contract(root), self.workflow(root, f"          ref: {'b' * 40}\n"))

    def test_mutable_contract_ref_is_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = pathlib.Path(temporary)
            with self.assertRaisesRegex(MODULE.ContractError, "immutable 40-character"):
                MODULE.build_report(
                    self.contract(root, ref="main", ref_kind="branch"),
                    self.workflow(root, "          ref: main\n"),
                )


if __name__ == "__main__":
    unittest.main()
