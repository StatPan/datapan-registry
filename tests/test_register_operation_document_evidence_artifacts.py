from __future__ import annotations

import hashlib
import importlib.util
import json
import pathlib
import tempfile
import unittest
from unittest import mock


ROOT = pathlib.Path(__file__).parents[1]
MODULE_PATH = ROOT / "scripts/register-operation-document-evidence-artifacts.py"
SPEC = importlib.util.spec_from_file_location("register_operation_document_evidence_artifacts", MODULE_PATH)
assert SPEC and SPEC.loader
REG = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(REG)


class RegisterOperationDocumentEvidenceArtifactsTest(unittest.TestCase):
    def _fixture(self, root: pathlib.Path) -> tuple[pathlib.Path, pathlib.Path, bytes]:
        evidence_dir = root / "reports/operation-document-evidence/v2"
        receipt_dir = evidence_dir / "receipts"
        receipt_dir.mkdir(parents=True)
        evidence_path = evidence_dir / "10001-9001.json"
        evidence = {
            "schema_version": "datapan.operation-document-evidence.v2",
            "identity": {
                "operation_id": "a" * 64,
                "dataset_id": "10001",
                "upstream_operation_key": "9001",
            },
        }
        evidence_raw = (json.dumps(evidence, separators=(",", ":")) + "\n").encode()
        evidence_path.write_bytes(evidence_raw)
        receipt = {
            "schema_version": "datapan.operation-document-capture-receipt.v2",
            "operation_id": "a" * 64,
            "status": "acquired",
            "evidence_sha256": hashlib.sha256(evidence_raw).hexdigest(),
        }
        (receipt_dir / evidence_path.name).write_text(json.dumps(receipt) + "\n", encoding="utf-8")
        return evidence_path, receipt_dir / evidence_path.name, evidence_raw

    def test_registers_digest_bound_sidecar_and_receipt_idempotently(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = pathlib.Path(temporary)
            self._fixture(root)
            with mock.patch.object(REG, "ROOT", root), mock.patch.object(REG, "EVIDENCE_DIR", root / "reports/operation-document-evidence/v2"), mock.patch.object(REG, "RECEIPT_DIR", root / "reports/operation-document-evidence/v2/receipts"):
                source = {"artifacts": [{"path": "manifest.json", "kind": "registry", "schema": "registry-schema"}]}
                expected, added = REG.registered_manifest(source)
                self.assertEqual(added, 2)
                self.assertEqual(len(source["artifacts"]), 1)
                self.assertEqual({item["kind"] for item in expected["artifacts"]}, {"registry", "operation_document_evidence", "operation_document_capture_receipt"})
                expected_again, added_again = REG.registered_manifest(expected)
                self.assertEqual(added_again, 0)
                self.assertEqual(expected_again, expected)

    def test_rejects_unbound_receipt(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = pathlib.Path(temporary)
            evidence_path, receipt_path, _ = self._fixture(root)
            receipt = json.loads(receipt_path.read_text())
            receipt["operation_id"] = "b" * 64
            receipt_path.write_text(json.dumps(receipt) + "\n", encoding="utf-8")
            with mock.patch.object(REG, "ROOT", root), mock.patch.object(REG, "EVIDENCE_DIR", evidence_path.parent), mock.patch.object(REG, "RECEIPT_DIR", receipt_path.parent):
                with self.assertRaisesRegex(REG.RegistrationError, "capture_receipt_binding_invalid"):
                    REG.registered_manifest({"artifacts": []})


if __name__ == "__main__":
    unittest.main()
