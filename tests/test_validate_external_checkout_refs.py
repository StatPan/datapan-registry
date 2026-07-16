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

    def workflow(self, root: pathlib.Path, body: str, name: str = "verify.yml") -> pathlib.Path:
        directory = root / "workflows"
        directory.mkdir(exist_ok=True)
        path = directory / name
        path.write_text(body, encoding="utf-8")
        return path

    def contract(
        self,
        root: pathlib.Path,
        workflows: list[pathlib.Path],
        checkout_count: int = 1,
        **overrides,
    ) -> pathlib.Path:
        counts = {path.as_posix(): 1 for path in workflows}
        value = {
            "schema_version": "datapan.external-checkout-refs.v1",
            "repository": "StatPan/datapan-cli",
            "ref_kind": "commit_sha",
            "ref": self.SHA,
            "inventory_expectations": {
                "checkout_count": checkout_count,
                "workflow_checkout_counts": counts,
            },
            "update_policy": {"mutable_refs_allowed": False},
        }
        value.update(overrides)
        path = root / "contract.json"
        path.write_text(json.dumps(value), encoding="utf-8")
        return path

    def canonical_step(self, *, ref: str | None = None) -> str:
        ref_line = "" if ref is None else f"          ref: {ref}\n"
        return (
            "jobs:\n"
            "  verify:\n"
            "    steps:\n"
            "      - name: Checkout CLI\n"
            "        uses: actions/checkout@v7\n"
            "        with:\n"
            "          repository: StatPan/datapan-cli\n"
            f"{ref_line}"
            "          path: datapan-cli\n"
        )

    def build(self, root: pathlib.Path, workflow: pathlib.Path, **contract_overrides):
        return MODULE.build_report(
            self.contract(root, [workflow], **contract_overrides), workflow.parent
        )

    def test_inventory_records_exact_reviewed_identity(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = pathlib.Path(temporary)
            workflow = self.workflow(root, self.canonical_step(ref=self.SHA))
            report = self.build(root, workflow)
            self.assertEqual(report["selected_ref"], self.SHA)
            self.assertEqual(report["checkout_count"], 1)
            checkout = report["workflows"][0]["checkouts"][0]
            self.assertEqual(checkout["ref"], self.SHA)
            self.assertEqual(checkout["job"], "verify")

    def test_unnamed_quoted_flow_mapping_checkout_is_discovered(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = pathlib.Path(temporary)
            workflow = self.workflow(
                root,
                "jobs:\n"
                "  verify:\n"
                "    steps:\n"
                '      - uses: "actions/checkout@v7"\n'
                f'        with: {{repository: "StatPan/datapan-cli", ref: "{self.SHA}", path: "datapan-cli"}}\n',
                name="verify.yaml",
            )
            report = self.build(root, workflow)
            self.assertEqual(report["checkout_count"], 1)
            self.assertEqual(report["workflows"][0]["checkouts"][0]["action"], "actions/checkout@v7")

    def test_missing_ref_is_rejected_before_default_branch_lookup(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = pathlib.Path(temporary)
            workflow = self.workflow(root, self.canonical_step())
            with self.assertRaisesRegex(MODULE.ContractError, "missing ref"):
                self.build(root, workflow)

    def test_ref_drift_is_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = pathlib.Path(temporary)
            workflow = self.workflow(root, self.canonical_step(ref="b" * 40))
            with self.assertRaisesRegex(MODULE.ContractError, "does not match reviewed identity"):
                self.build(root, workflow)

    def test_repository_case_variant_cannot_hide_checkout(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = pathlib.Path(temporary)
            workflow = self.workflow(
                root,
                self.canonical_step(ref=self.SHA).replace(
                    "StatPan/datapan-cli", "statpan/datapan-cli"
                ),
            )
            with self.assertRaisesRegex(MODULE.ContractError, "canonical spelling"):
                self.build(root, workflow)

    def test_mutable_contract_ref_is_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = pathlib.Path(temporary)
            workflow = self.workflow(root, self.canonical_step(ref="main"))
            contract = self.contract(root, [workflow], ref="main", ref_kind="branch")
            with self.assertRaisesRegex(MODULE.ContractError, "immutable 40-character"):
                MODULE.build_report(contract, workflow.parent)

    def test_anchors_and_aliases_are_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = pathlib.Path(temporary)
            workflow = self.workflow(
                root,
                "x-checkout: &checkout actions/checkout@v7\n"
                "jobs:\n"
                "  verify:\n"
                "    steps:\n"
                "      - uses: *checkout\n"
                f"        with: {{repository: StatPan/datapan-cli, ref: {self.SHA}}}\n",
            )
            with self.assertRaisesRegex(MODULE.ContractError, "anchors and aliases"):
                self.build(root, workflow)

    def test_merge_keys_are_rejected_without_aliases(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = pathlib.Path(temporary)
            workflow = self.workflow(
                root,
                "jobs:\n"
                "  verify:\n"
                "    steps:\n"
                "      - <<: {uses: actions/checkout@v7}\n"
                f"        with: {{repository: StatPan/datapan-cli, ref: {self.SHA}}}\n",
            )
            with self.assertRaisesRegex(MODULE.ContractError, "merge keys are not allowed"):
                self.build(root, workflow)

    def test_duplicate_step_and_input_keys_are_rejected(self):
        cases = {
            "uses": (
                "      - uses: actions/checkout@v7\n"
                "        uses: actions/checkout@v7\n"
                f"        with: {{repository: StatPan/datapan-cli, ref: {self.SHA}}}\n"
            ),
            "with": (
                "      - uses: actions/checkout@v7\n"
                f"        with: {{repository: StatPan/datapan-cli, ref: {self.SHA}}}\n"
                f"        with: {{repository: StatPan/datapan-cli, ref: {self.SHA}}}\n"
            ),
            "repository": (
                "      - uses: actions/checkout@v7\n"
                "        with:\n"
                "          repository: another/repository\n"
                "          repository: StatPan/datapan-cli\n"
                f"          ref: {self.SHA}\n"
            ),
            "ref": (
                "      - uses: actions/checkout@v7\n"
                "        with:\n"
                "          repository: StatPan/datapan-cli\n"
                "          ref: main\n"
                f"          ref: {self.SHA}\n"
            ),
            "path": (
                "      - uses: actions/checkout@v7\n"
                "        with:\n"
                "          repository: StatPan/datapan-cli\n"
                f"          ref: {self.SHA}\n"
                "          path: first\n"
                "          path: second\n"
            ),
        }
        for key, step in cases.items():
            with self.subTest(key=key), tempfile.TemporaryDirectory() as temporary:
                root = pathlib.Path(temporary)
                workflow = self.workflow(root, "jobs:\n  verify:\n    steps:\n" + step)
                with self.assertRaisesRegex(MODULE.ContractError, f"duplicate key '{key}'"):
                    self.build(root, workflow)

    def test_non_string_and_dynamic_checkout_identity_fields_fail_closed(self):
        steps = {
            "non_string_uses": (
                "      - uses: 7\n"
            ),
            "dynamic_uses": (
                "      - uses: ${{ matrix.action }}\n"
            ),
            "non_string_repository": (
                "      - uses: actions/checkout@v7\n"
                f"        with: {{repository: 7, ref: {self.SHA}}}\n"
            ),
            "dynamic_repository": (
                "      - uses: actions/checkout@v7\n"
                f"        with: {{repository: " + "${{ matrix.repository }}" + f", ref: {self.SHA}}}\n"
            ),
            "non_string_ref": (
                "      - uses: actions/checkout@v7\n"
                "        with: {repository: StatPan/datapan-cli, ref: 7}\n"
            ),
            "dynamic_ref": (
                "      - uses: actions/checkout@v7\n"
                "        with: {repository: StatPan/datapan-cli, ref: " + "${{ matrix.ref }}" + "}\n"
            ),
        }
        for name, step in steps.items():
            with self.subTest(name=name), tempfile.TemporaryDirectory() as temporary:
                root = pathlib.Path(temporary)
                workflow = self.workflow(root, "jobs:\n  verify:\n    steps:\n" + step)
                with self.assertRaises(MODULE.ContractError):
                    self.build(root, workflow)

    def test_extra_checkout_cannot_hide_outside_reviewed_count(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = pathlib.Path(temporary)
            step = (
                "      - uses: actions/checkout@v7\n"
                f"        with: {{repository: StatPan/datapan-cli, ref: {self.SHA}}}\n"
            )
            workflow = self.workflow(root, "jobs:\n  verify:\n    steps:\n" + step + step)
            with self.assertRaisesRegex(MODULE.ContractError, "checkout count drift"):
                self.build(root, workflow)

    def test_stale_inventory_is_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            output = pathlib.Path(temporary) / "inventory.json"
            output.write_text("{}\n", encoding="utf-8")
            with self.assertRaisesRegex(MODULE.ContractError, "inventory is stale"):
                MODULE.check_inventory(output, '{"expected": true}\n')

    def test_duplicate_contract_keys_are_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = pathlib.Path(temporary) / "contract.json"
            path.write_text('{"ref": "main", "ref": "' + self.SHA + '"}', encoding="utf-8")
            with self.assertRaisesRegex(MODULE.ContractError, "duplicate JSON contract key 'ref'"):
                MODULE.load_json(path)


if __name__ == "__main__":
    unittest.main()
