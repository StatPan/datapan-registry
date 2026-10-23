from __future__ import annotations

import copy
import importlib.util
import json
import pathlib
import shutil
import tempfile
import unittest
from unittest import mock


ROOT = pathlib.Path(__file__).parents[1]
MODULE_PATH = ROOT / "scripts/validate-pr762-historical-evidence.py"
SPEC = importlib.util.spec_from_file_location("validate_pr762_historical_evidence", MODULE_PATH)
assert SPEC and SPEC.loader
ARCHIVE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(ARCHIVE)

SOURCE_REF = "fabbeb5e33354ab4fb86905c0db282029bdd5f07"
MAIN_REF = "151da89679e34e3d7c034077c3a45a7c648b9de5"
ARCHIVE_REL = pathlib.Path("historical-evidence/pr-762-v2")


class ValidatePr762HistoricalEvidenceTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.temporary = tempfile.TemporaryDirectory(prefix="pr762-archive-test-")
        cls.root = pathlib.Path(cls.temporary.name)
        shutil.copytree(ROOT / ARCHIVE_REL, cls.root / ARCHIVE_REL)
        (cls.root / "manifest.json").write_bytes((ROOT / "manifest.json").read_bytes())
        for relative in (
            "schemas/datapan.operation-document-evidence.v2.schema.json",
            "schemas/datapan.operation-document-capture-receipt.v2.schema.json",
        ):
            target = cls.root / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes((ROOT / relative).read_bytes())

    @classmethod
    def tearDownClass(cls) -> None:
        cls.temporary.cleanup()

    def setUp(self) -> None:
        shutil.rmtree(self.root / ARCHIVE_REL)
        shutil.copytree(ROOT / ARCHIVE_REL, self.root / ARCHIVE_REL)
        self.inventory_path = self.root / ARCHIVE_REL / "inventory.json"
        self.metadata_path = self.root / ARCHIVE_REL / "metadata.json"

    def tearDown(self) -> None:
        pass

    def validate(self, *, source_ref: str | None = SOURCE_REF, main_ref: str | None = MAIN_REF):
        original_git_blob = ARCHIVE._git_blob
        original_git_text = ARCHIVE._git_text
        with mock.patch.object(ARCHIVE, "_git_blob", side_effect=lambda _root, ref, path: original_git_blob(ROOT, ref, path)), mock.patch.object(ARCHIVE, "_git_text", side_effect=lambda _root, *args: original_git_text(ROOT, *args)):
            return ARCHIVE.validate_archive(
                self.root,
                source_ref=source_ref,
                main_ref=main_ref,
                git_root=ROOT,
            )

    def load_inventory(self) -> dict:
        return json.loads(self.inventory_path.read_text(encoding="utf-8"))

    def write_inventory(self, inventory: dict) -> None:
        self.inventory_path.write_text(json.dumps(inventory, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    def load_metadata(self) -> dict:
        return json.loads(self.metadata_path.read_text(encoding="utf-8"))

    def write_metadata(self, metadata: dict) -> None:
        self.metadata_path.write_text(json.dumps(metadata, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    def test_valid_archive_resolves_full_refs_and_checks_all_overlap_bindings(self) -> None:
        result = self.validate()
        self.assertEqual(result["archive_files"], 466)
        self.assertEqual(result["pr_only_evidence"], 233)
        self.assertEqual(result["pr_only_receipts"], 233)
        self.assertEqual(result["overlap_evidence"], 167)
        self.assertEqual(result["overlap_receipts"], 167)
        self.assertEqual(result["status"], "pass")

    def test_commit_labels_cannot_both_be_reassigned_to_main(self) -> None:
        metadata = self.load_metadata()
        inventory = self.load_inventory()
        metadata["main_commit"] = MAIN_REF
        metadata["pr762_head_commit"] = MAIN_REF
        inventory["main_commit"] = MAIN_REF
        inventory["pr762_head_commit"] = MAIN_REF
        self.write_metadata(metadata)
        self.write_inventory(inventory)
        with self.assertRaisesRegex(ARCHIVE.ArchiveError, "commit_provenance_mismatch"):
            self.validate()

    def test_source_and_main_roles_cannot_be_swapped(self) -> None:
        with self.assertRaisesRegex(ARCHIVE.ArchiveError, "commit_provenance_mismatch"):
            self.validate(source_ref=MAIN_REF, main_ref=SOURCE_REF)

    def test_git_refs_are_required_for_success(self) -> None:
        with self.assertRaisesRegex(ARCHIVE.ArchiveError, "git_refs_required"):
            ARCHIVE.validate_archive(self.root, git_root=ROOT)

    def test_overlap_each_declared_binding_field_is_checked(self) -> None:
        fields_and_errors = (
            ("bytes", "overlap_bytes_mismatch"),
            ("main_sha256", "overlap_main_sha256_mismatch"),
            ("pr_sha256", "overlap_pr_sha256_mismatch"),
            ("git_blob_sha1", "overlap_main_blob_sha1_mismatch"),
            ("pr_git_blob_sha1", "overlap_pr_blob_sha1_mismatch"),
        )
        original = self.load_inventory()
        for field, error in fields_and_errors:
            with self.subTest(field=field):
                inventory = copy.deepcopy(original)
                inventory["overlap"][0][field] = "0" if field == "bytes" else "0" * len(str(inventory["overlap"][0][field]))
                self.write_inventory(inventory)
                with self.assertRaisesRegex(ARCHIVE.ArchiveError, error):
                    self.validate()
                self.setUp()

    def test_evidence_byte_mutation_fails_and_original_is_restored(self) -> None:
        inventory = self.load_inventory()
        record = next(row for row in inventory["files"] if row["kind"] == "evidence")
        path = self.root / record["archive_path"]
        original = path.read_bytes()
        path.unlink()
        path.write_bytes(original + b"\n")
        with self.assertRaisesRegex(ARCHIVE.ArchiveError, "archive_digest_mismatch"):
            self.validate()
        path.unlink()
        path.write_bytes(original)
        self.assertEqual(path.read_bytes(), original)

    def test_receipt_operation_id_mutation_fails(self) -> None:
        inventory = self.load_inventory()
        evidence = next(row for row in inventory["files"] if row["kind"] == "evidence")
        receipt = next(row for row in inventory["files"] if row["kind"] == "receipt" and pathlib.Path(row["source_path"]).name == pathlib.Path(evidence["source_path"]).name)
        path = self.root / receipt["archive_path"]
        value = json.loads(path.read_text(encoding="utf-8"))
        value["operation_id"] = "0" * 64 if value["operation_id"] != "0" * 64 else "1" * 64
        path.write_text(json.dumps(value) + "\n", encoding="utf-8")
        with self.assertRaisesRegex(ARCHIVE.ArchiveError, "receipt_operation_binding_invalid"):
            self.validate()

    def test_receipt_evidence_digest_mutation_fails(self) -> None:
        inventory = self.load_inventory()
        evidence = next(row for row in inventory["files"] if row["kind"] == "evidence")
        receipt = next(row for row in inventory["files"] if row["kind"] == "receipt" and pathlib.Path(row["source_path"]).name == pathlib.Path(evidence["source_path"]).name)
        path = self.root / receipt["archive_path"]
        value = json.loads(path.read_text(encoding="utf-8"))
        value["evidence_sha256"] = "0" * 64
        path.write_text(json.dumps(value) + "\n", encoding="utf-8")
        with self.assertRaisesRegex(ARCHIVE.ArchiveError, "receipt_evidence_digest_invalid"):
            self.validate()

    def test_inventory_evidence_digest_mutation_fails(self) -> None:
        inventory = self.load_inventory()
        inventory["files"][0]["sha256"] = "0" * 64
        self.write_inventory(inventory)
        with self.assertRaisesRegex(ARCHIVE.ArchiveError, "archive_digest_mismatch"):
            self.validate()


if __name__ == "__main__":
    unittest.main()
