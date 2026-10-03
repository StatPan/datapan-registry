from __future__ import annotations

import hashlib
import json
import pathlib
import sys
import tempfile
import unittest


SCRIPTS = pathlib.Path(__file__).parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))
import runtime_evidence_projection as projection  # noqa: E402


AS_OF = "2026-09-30T00:00:00Z"
DATASET_ID = "dataset-1"
OPERATION_NAME = "same-name"
OPERATION_KEY = "operation-1"


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def registry(*, endpoint: str = "http://api.example.test/v1/search", method: str = "GET", key: str = OPERATION_KEY,
             default_params: dict | None = None, request_params: list[dict] | None = None,
             operation_name: str = OPERATION_NAME) -> list[dict]:
    raw = {
        "id": "upstream-api-1",
        "list_id": DATASET_ID,
        "operation_seq": key,
        "api_type": "REST",
        "request_param_nm_en": "query",
        "response_param_nm_en": "items",
    }
    operation = {
        "name": operation_name,
        "endpoint": endpoint,
        "method": method,
        "request_params": request_params if request_params is not None else [{"name": "query", "label": "Query"}],
        "response_params": [{"name": "items", "label": "Items"}],
        "source": {"system": "data.go.kr", "raw": raw},
    }
    if default_params is not None:
        operation["default_params"] = default_params
    return [{"id": DATASET_ID, "source": {"raw": {"id": DATASET_ID, "list_id": DATASET_ID}}, "operations": [operation]}]


def operation_manifest(registry_rows: list[dict], source_sha256: str, *, endpoint: str | None = None,
                       method: str = "GET", action: str | None = None, operation_key: str | None = None,
                       operation_name: str = OPERATION_NAME, operation_id: str = "manifest-operation-1") -> dict:
    dataset_id = registry_rows[0]["id"]
    op = registry_rows[0]["operations"][0]
    raw = op["source"]["raw"]
    source_system = op["source"]["system"]
    if endpoint is None:
        endpoint = op["endpoint"]
    return {
        "schema_version": "datapan.data-go-kr-operation-manifest.v1",
        "source_snapshot": {"path": "data/data-go-kr.registry.json", "sha256": source_sha256},
        "operations": [{
            "operation_id": operation_id,
            "protocol": "REST",
            "provenance": {
                "provider": "data.go.kr",
                "dataset_id": dataset_id,
                "operation_name": operation_name,
                "source_system": source_system,
                "upstream_operation_key": operation_key or str(raw["operation_seq"]),
            },
            "transport": {"endpoint": endpoint, "method": method, "action": action, "method_evidence": "registry_default_get"},
            "requirements": {"all_request_parameters": [{"name": "query", "label": "Query"}]},
        }],
    }


def contract_for(registry_rows: list[dict], manifest: dict) -> dict:
    entry = manifest["operations"][0]
    return projection.operation_contract(registry_rows[0], registry_rows[0]["operations"][0], 0, entry)


def result_for(contract: dict, *, status: str = "verified", verified_at: str | None = "2026-09-29T00:00:00Z",
               run_id: str = "run-1", plan_sha256: str = "a" * 64,
               source_snapshot_sha256: str = "1" * 64, operation_manifest_sha256: str = "2" * 64) -> dict:
    plan_binding = projection.make_contract_binding(
        contract,
        source_snapshot_sha256=source_snapshot_sha256,
        operation_manifest_sha256=operation_manifest_sha256,
        identity_key=contract["identity_key"],
    )
    binding = projection.bind_result_to_plan(plan_binding, run_id=run_id, plan_sha256=plan_sha256)
    row = {
        "provider": "data.go.kr",
        "dataset_id": contract["dataset_id"],
        "operation": contract["operation"],
        "identity_key": contract["identity_key"],
        "status": status,
        "contract_binding": binding,
    }
    if verified_at is not None:
        row["verified_at"] = verified_at
    return row


class RuntimeEvidenceProjectionTest(unittest.TestCase):
    def build(self, registry_rows: list[dict], manifest: dict, row: dict | None, *, trusted: bool = True,
              provenance_row: dict | None = None, as_of: str = AS_OF) -> dict:
        registry_sha = manifest["source_snapshot"]["sha256"]
        manifest_sha = "3" * 64
        latest = {"generated_at": as_of, "results": [row] if row is not None else []}
        trusted_index = {}
        proven_row = provenance_row if provenance_row is not None else row
        if trusted and proven_row and isinstance(proven_row.get("contract_binding"), dict):
            binding = proven_row["contract_binding"]
            trusted_index[(binding["run_id"], binding["identity_key"])] = (
                projection.digest(binding), projection.digest(proven_row)
            )
        return projection.build_projection(
            registry_rows,
            latest,
            manifest,
            registry_sha256=registry_sha,
            operation_manifest_sha256=manifest_sha,
            latest_sha256=projection.digest(latest),
            provenance_bindings=trusted_index,
            provenance_summary={"admitted_runs": 1 if trusted_index else 0, "attested_runs": 1 if trusted_index else 0, "pending_attestation_runs": 0, "invalid_runs": 0, "proven_row_bindings": len(trusted_index)},
        )

    def test_exact_binding_survives_global_source_snapshot_change_when_operation_contract_is_unchanged(self) -> None:
        old_sha, current_sha = "4" * 64, "5" * 64
        old_registry = registry()
        old_manifest = operation_manifest(old_registry, old_sha)
        old_contract = contract_for(old_registry, old_manifest)
        row = result_for(old_contract, source_snapshot_sha256=old_sha, operation_manifest_sha256="6" * 64)
        current_registry = registry()
        current_manifest = operation_manifest(current_registry, current_sha)

        result = self.build(current_registry, current_manifest, row)

        self.assertEqual(result["summary"]["eligible"], 1)
        self.assertEqual(result["summary"]["current_operations_with_fresh_verified_evidence"], 1)
        self.assertNotEqual(row["contract_binding"]["source_snapshot_sha256"], result["inputs"]["registry_source_sha256"])

    def test_endpoint_method_action_default_and_parameter_changes_invalidate_old_binding(self) -> None:
        source_sha = "7" * 64
        original_registry = registry(default_params={"pageNo": "1"})
        original_manifest = operation_manifest(original_registry, source_sha)
        original_contract = contract_for(original_registry, original_manifest)
        row = result_for(original_contract)

        variants = [
            (registry(endpoint="https://api.example.test/v1/search", default_params={"pageNo": "1"}), "https://api.example.test/v1/search", "GET", None, OPERATION_KEY, OPERATION_NAME),
            (registry(endpoint="http://api.example.test/v2/search", default_params={"pageNo": "1"}), "http://api.example.test/v2/search", "GET", None, OPERATION_KEY, OPERATION_NAME),
            (registry(method="POST", default_params={"pageNo": "1"}), "http://api.example.test/v1/search", "POST", None, OPERATION_KEY, OPERATION_NAME),
            (registry(default_params={"pageNo": "2"}), "http://api.example.test/v1/search", "GET", None, OPERATION_KEY, OPERATION_NAME),
            (registry(request_params=[{"name": "query", "label": "Query", "schema": {"type": "integer"}}], default_params={"pageNo": "1"}), "http://api.example.test/v1/search", "GET", None, OPERATION_KEY, OPERATION_NAME),
            (registry(default_params={"pageNo": "1"}), "http://api.example.test/v1/search", "GET", "fetch-v2", OPERATION_KEY, OPERATION_NAME),
            (registry(default_params={"pageNo": "1"}, key="operation-2"), "http://api.example.test/v1/search", "GET", None, "operation-2", OPERATION_NAME),
        ]
        for current_registry, endpoint, method, action, key, name in variants:
            with self.subTest(endpoint=endpoint, method=method, action=action, operation_key=key):
                current_manifest = operation_manifest(
                    current_registry,
                    "8" * 64,
                    endpoint=endpoint,
                    method=method,
                    action=action,
                    operation_key=key,
                    operation_name=name,
                    operation_id="changed-manifest-operation",
                )
                result = self.build(current_registry, current_manifest, row)
                self.assertEqual(result["summary"]["contract_changed"], 1)
                self.assertEqual(result["summary"]["current_operations_with_fresh_verified_evidence"], 0)

    def test_soap_action_is_a_complete_transport_contract_without_http_method(self) -> None:
        rows = registry()
        operation = rows[0]["operations"][0]
        operation.pop("method")
        operation["source"]["raw"]["api_type"] = "SOAP"
        manifest = operation_manifest(rows, "a" * 64)
        manifest_operation = manifest["operations"][0]
        manifest_operation["protocol"] = "SOAP"
        manifest_operation["transport"] = {
            "endpoint": "https://api.example.test/soap/SearchService",
            "method": None,
            "action": "searchItems",
            "method_evidence": "soap_action",
        }

        contract = contract_for(rows, manifest)

        self.assertTrue(contract["contract_complete"], contract["incomplete_reasons"])
        self.assertEqual(contract["endpoint"]["path"], "/soap/SearchService")
        self.assertTrue(contract["method_action_sha256"])

    def test_duplicate_names_and_missing_binding_fail_closed(self) -> None:
        rows = registry()
        duplicate = json.loads(json.dumps(rows[0]["operations"][0]))
        duplicate["source"]["raw"]["operation_seq"] = "operation-2"
        rows[0]["operations"].append(duplicate)
        manifest = operation_manifest(rows, "9" * 64)
        other = json.loads(json.dumps(manifest["operations"][0]))
        other["operation_id"] = "manifest-operation-2"
        other["provenance"]["upstream_operation_key"] = "operation-2"
        manifest["operations"].append(other)
        legacy = {"dataset_id": DATASET_ID, "operation": OPERATION_NAME, "provider": "data.go.kr", "status": "verified", "verified_at": "2026-09-29T00:00:00Z", "endpoint_host": "api.example.test"}

        result = self.build(rows, manifest, legacy, trusted=False)

        self.assertEqual(result["summary"]["ambiguous"], 1)
        self.assertEqual(result["summary"]["current_operations_with_bound_evidence"], 0)

        unique_rows = registry()
        unique_manifest = operation_manifest(unique_rows, "a" * 64)
        unique_legacy = dict(legacy)
        unbound = self.build(unique_rows, unique_manifest, unique_legacy, trusted=False)
        self.assertEqual(unbound["summary"]["unbound"], 1)
        self.assertEqual(unbound["summary"]["current_operations_with_fresh_verified_evidence"], 0)

    def test_expiry_and_row_digest_prevent_timestamp_or_status_reuse(self) -> None:
        current_registry = registry()
        current_manifest = operation_manifest(current_registry, "b" * 64)
        contract = contract_for(current_registry, current_manifest)
        old_row = result_for(contract, verified_at="2026-06-01T00:00:00Z")
        expired = self.build(current_registry, current_manifest, old_row, as_of=AS_OF)
        self.assertEqual(expired["summary"]["expired"], 1)
        self.assertEqual(expired["summary"]["current_operations_within_expiry_window"], 0)

        recent = result_for(contract)
        edited = dict(recent, status="failed", verified_at="2026-09-28T00:00:00Z")
        tampered = self.build(current_registry, current_manifest, edited, provenance_row=recent, trusted=True)
        self.assertEqual(tampered["summary"]["unbound"], 1)
        self.assertEqual(tampered["summary"]["eligible"], 0)

    def test_immutably_bound_failed_and_skipped_rows_are_observations_not_successes(self) -> None:
        current_registry = registry()
        current_manifest = operation_manifest(current_registry, "b" * 64)
        contract = contract_for(current_registry, current_manifest)

        for status in ("failed", "skipped"):
            with self.subTest(status=status):
                row = result_for(contract, status=status)
                result = self.build(current_registry, current_manifest, row)

                self.assertEqual(result["summary"]["recent_non_verified"], 1)
                self.assertEqual(result["summary"]["current_operations_with_bound_evidence"], 1)
                self.assertEqual(result["summary"]["current_operations_with_fresh_verified_evidence"], 0)
                self.assertEqual(result["current_evidence"][0]["status"], status)
                self.assertEqual(result["current_evidence"][0]["disposition"], "recent_non_verified")

    def test_attested_import_index_requires_binding_and_full_result_digests(self) -> None:
        contract = contract_for(registry(), operation_manifest(registry(), "c" * 64))
        result = result_for(contract)
        binding = result["contract_binding"]
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            receipt_path = root / "reports/runtime-freshness-imports/run-1.json"
            admission_path = root / "reports/runtime-freshness-import-admissions/run-1.json"
            attestation_path = root / "reports/runtime-freshness-import-attestations/run-1.json"
            for path in (receipt_path, admission_path, attestation_path):
                path.parent.mkdir(parents=True, exist_ok=True)
            receipt = {"run_id": "run-1", "inputs": {"sanitized_report_sha256": "d" * 64, "run_receipt_sha256": "e" * 64}, "results": [{"identity_key": contract["identity_key"], "contract_binding_sha256": projection.digest(binding), "result_sha256": projection.digest(result)}]}
            receipt_bytes = (json.dumps(receipt, sort_keys=True) + "\n").encode()
            receipt_path.write_bytes(receipt_bytes)
            admission = {"run_id": "run-1", "producer": {}, "inputs": {"sanitized_report_sha256": "d" * 64, "run_receipt_sha256": "e" * 64, "import_receipt": {"path": "reports/runtime-freshness-imports/run-1.json", "sha256": sha(receipt_bytes)}}}
            admission_bytes = (json.dumps(admission, sort_keys=True) + "\n").encode()
            admission_path.write_bytes(admission_bytes)
            attestation = {"run_id": "run-1", "lineage": {"admission": {"sha256": sha(admission_bytes)}, "import_receipt": {"sha256": sha(receipt_bytes)}, "sanitized_report_sha256": "d" * 64, "run_receipt_sha256": "e" * 64}}
            attestation_path.write_text(json.dumps(attestation, sort_keys=True) + "\n", encoding="utf-8")

            bindings, counts = projection.immutable_import_binding_index(root)

        self.assertEqual(bindings[("run-1", contract["identity_key"])], (projection.digest(binding), projection.digest(result)))
        self.assertEqual(counts["attested_runs"], 1)
        self.assertEqual(counts["proven_row_bindings"], 1)

    def test_immutable_import_index_rejects_missing_attestation_and_receipt_digest_tampering(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            receipt_path = root / "reports/runtime-freshness-imports/run-1.json"
            admission_path = root / "reports/runtime-freshness-import-admissions/run-1.json"
            attestation_path = root / "reports/runtime-freshness-import-attestations/run-1.json"
            for path in (receipt_path, admission_path, attestation_path):
                path.parent.mkdir(parents=True, exist_ok=True)
            receipt = {"run_id": "run-1", "inputs": {"sanitized_report_sha256": "d" * 64, "run_receipt_sha256": "e" * 64}, "results": []}
            receipt_bytes = (json.dumps(receipt, sort_keys=True) + "\n").encode()
            receipt_path.write_bytes(receipt_bytes)
            admission = {"run_id": "run-1", "producer": {}, "inputs": {"sanitized_report_sha256": "d" * 64, "run_receipt_sha256": "e" * 64, "import_receipt": {"path": "reports/runtime-freshness-imports/run-1.json", "sha256": sha(receipt_bytes)}}}
            admission_bytes = (json.dumps(admission, sort_keys=True) + "\n").encode()
            admission_path.write_bytes(admission_bytes)
            attestation = {"run_id": "run-1", "lineage": {"admission": {"sha256": sha(admission_bytes)}, "import_receipt": {"sha256": sha(receipt_bytes)}, "sanitized_report_sha256": "d" * 64, "run_receipt_sha256": "e" * 64}}
            attestation_bytes = (json.dumps(attestation, sort_keys=True) + "\n").encode()
            attestation_path.write_bytes(attestation_bytes)

            attestation_path.unlink()
            missing, missing_counts = projection.immutable_import_binding_index(root)
            self.assertEqual(missing, {})
            self.assertEqual(missing_counts["pending_attestation_runs"], 1)

            admission["inputs"]["import_receipt"]["sha256"] = "f" * 64
            tampered_admission_bytes = (json.dumps(admission, sort_keys=True) + "\n").encode()
            admission_path.write_bytes(tampered_admission_bytes)
            attestation["lineage"]["admission"]["sha256"] = sha(tampered_admission_bytes)
            attestation["lineage"]["import_receipt"]["sha256"] = "f" * 64
            attestation_path.write_text(json.dumps(attestation, sort_keys=True) + "\n", encoding="utf-8")
            tampered, tampered_counts = projection.immutable_import_binding_index(root)

        self.assertEqual(tampered, {})
        self.assertEqual(tampered_counts["invalid_runs"], 1)


if __name__ == "__main__":
    unittest.main()
