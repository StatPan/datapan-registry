from __future__ import annotations

import copy
import importlib.util
import json
from pathlib import Path
import subprocess
import tempfile
import unittest

import jsonschema


ROOT = Path(__file__).resolve().parents[1]
COMPILER_PATH = ROOT / "scripts/generate-operation-observation-plan.py"
SCHEMA_PATH = ROOT / "schemas/datapan.operation-observation-plan.v1.schema.json"
REGISTRAR_PATH = ROOT / "scripts/register-operation-observation-plan-artifacts.py"


def load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class GenerateOperationObservationPlanTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.compiler = load_module("operation_observation_plan", COMPILER_PATH)
        cls.schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
        cls.validator = jsonschema.Draft202012Validator(cls.schema, format_checker=jsonschema.FormatChecker())

    def test_current_registered_operation_sets_reconcile_exactly(self):
        index, outputs = self.compiler.build(ROOT)
        summary = index["summary"]
        self.assertEqual(summary["known_operations"], 12666)
        self.assertEqual(summary["known_operations"], sum(scope["registered_operations"] for scope in index["source_scopes"]))
        self.assertEqual(summary["request_plans_complete"], 19)
        self.assertEqual(summary["request_plans_incomplete"], 12647)
        self.assertEqual(summary["runtime_bindings_bound"], 0)
        self.assertEqual(summary["runtime_bindings_unbound"], 12666)
        self.assertEqual(summary["admitted"], 0)
        self.assertEqual(summary["not_admitted"], 12666)
        self.assertEqual(summary["inventory_unknown_scopes"], 4)
        self.assertEqual(index["inventory_context"]["separate_link_operations"], 8871)
        self.assertEqual(index["inventory_context"]["provider_index_adapter_entries"], 138)
        self.assertFalse(index["inventory_context"]["provider_index_entries_counted_as_operations"])
        self.assertEqual(
            [(scope["source_id"], scope["registered_operations"], scope["inventory_status"], scope["inventory_unknown"]) for scope in index["source_scopes"]],
            [
                ("data_go_kr", 12662, "source_complete", False),
                ("ecos", 1, "partial", True),
                ("kosis", 1, "partial", True),
                ("open_assembly", 1, "partial", True),
                ("seoul_open_data", 1, "partial", True),
            ],
        )
        self.validator.validate(index)
        self.assertEqual(index["registry_revision"], self.compiler.current_revision(ROOT))
        seoul_plan = next(
            record
            for path, raw in outputs.items()
            if "/shards/" in path
            for record in json.loads(raw)["records"]
            if record["operation_identity"]["operation_id"] == "seoul-open-data-subway-station-list"
        )
        self.assertEqual(seoul_plan["operation_identity"]["registered_endpoint"]["port"], 8088)
        self.assertIn("secure_transport_required", seoul_plan["request_plan"]["missing_fields"])
        endpoint_missing_plan = next(
            record
            for path, raw in outputs.items()
            if "/shards/" in path
            for record in json.loads(raw)["records"]
            if record["operation_identity"]["operation_id"] == "0465a66688ad765264b2aa5f116098b2877f37d1a0ab5ecbd1d0be14fcff4424"
        )
        self.assertNotIn("registered_endpoint", endpoint_missing_plan["operation_identity"])
        self.assertIn("registered_endpoint_missing", endpoint_missing_plan["request_plan"]["missing_fields"])
        self.assertTrue(all(item["bytes"] > 0 and len(item["sha256"]) == 64 for item in index["generation_inputs"]["document_evidence"]))
        data_go_artifacts = {ref["path"] for scope in index["source_scopes"] if scope["source_id"] == "data_go_kr" for ref in scope["source_artifacts"]}
        self.assertTrue({
            "policy/health-probe-canaries.json",
            "policy/operation-observation-policies.v1.json",
            "data/provider-index.json",
            "schemas/datapan.operation-observation-policy.v1.schema.json",
            "schemas/datapan.operation-response-assertion.v2.schema.json",
            "schemas/datapan.operation-document-evidence.v1.schema.json",
            "schemas/datapan.operation-document-evidence.v2.schema.json",
            "schemas/datapan.operation-document-capture-receipt.v2.schema.json",
            "schemas/datapan.operation-document-work-item.v2.schema.json",
            "schemas/datapan.operation-document-reconciliation.v2.schema.json",
            "reports/operation-document-evidence/queue.v2.jsonl",
            "reports/operation-document-evidence/reconciliation.v2.json",
            "scripts/operation_document_evidence.py",
            "scripts/generate-operation-observation-plan.py",
        }.issubset(data_go_artifacts))
        shard_paths = [path for path in outputs if "/shards/" in path]
        self.assertTrue(shard_paths)
        for path in shard_paths:
            shard = json.loads(outputs[path])
            self.validator.validate(shard)
            ids = [record["operation_identity"]["operation_id"] for record in shard["records"]]
            self.assertLessEqual(len(ids), 256)
            self.assertEqual(ids, sorted(ids))
            self.assertTrue(all("source_artifacts" not in record["source_binding"] for record in shard["records"]))

    def test_all_owned_schema_local_references_resolve(self):
        schema_paths = sorted((ROOT / "schemas").glob("datapan.*.schema.json"))
        self.assertGreater(len(schema_paths), 0)
        for path in schema_paths:
            schema = json.loads(path.read_text(encoding="utf-8"))
            jsonschema.Draft202012Validator.check_schema(schema)

            def visit(value):
                if isinstance(value, dict):
                    reference = value.get("$ref")
                    if isinstance(reference, str) and reference.startswith("#/"):
                        with self.subTest(schema=path.name, ref=reference):
                            self.compiler.json_pointer_value(schema, reference)
                    for child in value.values():
                        visit(child)
                elif isinstance(value, list):
                    for child in value:
                        visit(child)

            visit(schema)

    def test_registered_endpoint_preserves_only_explicit_ports(self):
        self.assertEqual(
            self.compiler.registered_endpoint("http://openapi.seoul.go.kr:8088/example"),
            {"host": "openapi.seoul.go.kr", "port": 8088, "path": "/example"},
        )
        self.assertEqual(
            self.compiler.registered_endpoint("https://example.invalid/path"),
            {"host": "example.invalid", "path": "/path"},
        )
        self.assertEqual(
            self.compiler.registered_endpoint("https://example.invalid:443/path"),
            {"host": "example.invalid", "port": 443, "path": "/path"},
        )
        self.assertTrue(
            self.compiler.registered_paths_match(
                "/{KEY}/{TYPE}/{SERVICE}/{START_INDEX}/{END_INDEX}",
                "/{KEY}/{format}/{service}/{start_index}/{end_index}",
            )
        )
        self.assertFalse(
            self.compiler.registered_paths_match(
                "/{KEY}/station/{SERVICE}",
                "/{KEY}/bus/{SERVICE}",
            )
        )

    def test_release_manifest_closes_over_plan_inputs_and_document_sidecars(self):
        registrar = load_module("operation_observation_manifest_registrar", REGISTRAR_PATH)
        manifest = json.loads((ROOT / "manifest.json").read_text(encoding="utf-8"))
        expected = registrar.expected_manifest(manifest)
        rows = expected["artifacts"]
        paths = {row["path"] for row in rows}
        index = json.loads((ROOT / "reports/operation-observation-plan/index.json").read_text(encoding="utf-8"))
        referenced = {
            ref["path"]
            for scope in index["source_scopes"]
            for ref in scope["source_artifacts"]
        }
        referenced.update(
            ref["path"]
            for ref in index["generation_inputs"]["document_evidence"]
        )
        self.assertTrue(referenced.issubset(paths))
        evidence_paths = {
            path.relative_to(ROOT).as_posix()
            for path in (ROOT / "reports/operation-document-evidence").rglob("*")
            if path.is_file()
        }
        self.assertTrue(evidence_paths.issubset(paths))
        self.assertIn("scripts/generate-operation-observation-plan.py", paths)
        self.assertIn("scripts/operation_document_evidence.py", paths)
        self.assertIn("policy/operation-observation-policies.v1.json", paths)
        schema_index_rows = [row for row in rows if row["path"] == "schemas/index.json"]
        self.assertEqual(len(schema_index_rows), 1)
        self.assertEqual(schema_index_rows[0]["kind"], "schema_index")
        schema_paths = [entry["path"] for entry in json.loads((ROOT / "schemas/index.json").read_text(encoding="utf-8"))["schemas"]]
        self.assertEqual([row["path"] for row in rows if row["kind"] == "schema"], schema_paths)
        self.assertIn("reports/operation-response-assertions/fee64750123f617a1c230a30f012a0f3330ede1cc92587716d6ad386c37da0bb.json", paths)
        release_schema = json.loads((ROOT / "schemas/datapan.release-manifest.v1.schema.json").read_text(encoding="utf-8"))
        jsonschema.Draft202012Validator(release_schema, format_checker=jsonschema.FormatChecker()).validate(expected)

    def test_real_operation_document_evidence_reconciles_canary_and_noncanary_without_admission(self):
        index, outputs = self.compiler.build(ROOT)
        expected_identities = {
            ("15001697", "24807", "5627942fc456d69230e6097c1fa98fe72c74ca34b01cf5b488af039323f25abf"),
            ("15001808", "16811", "655adc96663128905bf0f778af0b4e00311abce240bb5d7d09f0a6a40ce81e03"),
        }
        document_inputs = index["generation_inputs"]["document_evidence"]
        self.assertEqual(len(document_inputs), 171)
        self.assertTrue(any(item["path"].endswith("15001697-24807.json") for item in document_inputs))
        self.assertTrue(any(item["path"].endswith("15001808-16811.json") for item in document_inputs))
        self.assertTrue(all(item["bytes"] > 0 and len(item["sha256"]) == 64 for item in document_inputs))
        records = [
            record
            for path, raw in outputs.items()
            if "/shards/" in path
            for record in json.loads(raw)["records"]
        ]
        by_id = {record["operation_identity"]["operation_id"]: record for record in records}
        self.assertTrue(expected_identities.issubset({
            (
                record["operation_identity"].get("dataset_id"),
                record["operation_identity"].get("upstream_operation_key"),
                record["operation_identity"]["operation_id"],
            )
            for record in records
        }))
        for dataset_id, upstream_key, operation_id in expected_identities:
            record = by_id[operation_id]
            self.assertEqual(record["operation_identity"]["dataset_id"], dataset_id)
            self.assertEqual(record["operation_identity"]["upstream_operation_key"], upstream_key)
            self.assertEqual(record["request_plan"]["status"], "incomplete")
            self.assertEqual(record["runtime_binding"]["status"], "unbound")
            self.assertEqual(record["admission"]["status"], "not_admitted")
            self.assertNotIn("request_contract", record["request_plan"])
            self.assertNotIn("operation_effect_read_only_authority", record["request_plan"]["missing_fields"])
            self.assertIn("safe_value_strategy_for_required_inputs", record["request_plan"]["missing_fields"])
            refs = [ref for ref in record["request_plan"]["evidence_refs"] if ref["evidence_kind"] == "operation_document"]
            self.assertTrue(refs)
            self.assertTrue(any(ref["json_pointer"] == "#/effect" for ref in refs))
            self.assertTrue(any(ref["json_pointer"] == "#/transport/http_method" for ref in refs))
            self.assertTrue(all(ref["artifact_path"].endswith(f"{dataset_id}-{upstream_key}.json") for ref in refs))
            sidecars = {
                ref["artifact_path"]: json.loads((ROOT / ref["artifact_path"]).read_text(encoding="utf-8"))
                for ref in refs
            }
            sidecar = sidecars[refs[0]["artifact_path"]]
            self.assertEqual(sidecar["transport"]["http_method"]["value"], None)
            self.assertEqual(sidecar["transport"]["http_method"]["status"], "unknown")
            self.assertEqual(sidecar["effect"]["classification"], "read_only")
            self.assertTrue(
                all(
                    self.compiler.json_pointer_value(sidecars[ref["artifact_path"]], ref["json_pointer"]) is not None
                    for ref in refs
                )
            )

        noncanary = by_id["655adc96663128905bf0f778af0b4e00311abce240bb5d7d09f0a6a40ce81e03"]
        self.assertNotIn("legacy_selectors", noncanary["operation_identity"])

    def test_documented_safe_request_compiles_observation_only_without_claiming_response_health(self):
        index, outputs = self.compiler.build(ROOT)
        records = [
            record
            for path, raw in outputs.items()
            if "/shards/" in path
            for record in json.loads(raw)["records"]
        ]
        operation_id = "fee64750123f617a1c230a30f012a0f3330ede1cc92587716d6ad386c37da0bb"
        record = next(row for row in records if row["operation_identity"]["operation_id"] == operation_id)
        self.assertEqual(record["request_plan"]["status"], "complete")
        self.assertEqual(record["runtime_binding"]["status"], "unbound")
        self.assertEqual(record["admission"]["status"], "not_admitted")
        contract = record["request_plan"]["request_contract"]
        self.assertEqual(contract["transport"]["http_method"], "GET")
        self.assertEqual(contract["transport"]["authority"], "operation_document")
        self.assertEqual(contract["operation_effect"]["authority"], "reviewed_policy")
        self.assertEqual(contract["authentication"]["requirement"], "required")
        self.assertEqual(contract["authentication"]["placement"], "query")
        self.assertEqual([parameter["name"] for parameter in contract["parameters"]], ["serviceKey"])
        self.assertEqual(contract["parameters"][0]["value_strategy"], {
            "kind": "credential_reference",
            "authority": "runtime_binding",
            "binding_field": "credential_reference",
        })
        response_assertion = contract["response_assertion"]
        self.assertEqual(response_assertion["kind"], "observation_only")
        self.assertEqual(response_assertion["empty_result_semantics"], "not_applicable")
        self.assertNotIn("expected_status_codes", response_assertion)
        refs = response_assertion["evidence_refs"]
        evidence = {(ref["artifact_path"], ref["json_pointer"], ref["evidence_kind"]) for ref in refs}
        evidence_file = "reports/operation-document-evidence/v2/15158559-69640.json"
        self.assertIn((evidence_file, "#/transport/http_method", "operation_document"), evidence)
        self.assertIn((evidence_file, "#/operation_document/title", "operation_document"), evidence)
        self.assertIn((evidence_file, "#/operation_document/purpose", "operation_document"), evidence)
        self.assertIn((evidence_file, "#/authentication", "operation_document"), evidence)
        self.assertIn((evidence_file, "#/parameters", "operation_document"), evidence)
        self.assertIn((evidence_file, "#/response_contract", "operation_document"), evidence)
        self.assertIn(("policy/operation-observation-policies.v1.json", "#/policies/0/request/response_assertion_artifact", "reviewed_policy"), evidence)
        self.assertIn(("policy/operation-observation-policies.v1.json", "#/policies/0/review", "reviewed_policy"), evidence)
        assertion_file = ROOT / f"reports/operation-response-assertions/{operation_id}.json"
        assertion_artifact = json.loads(assertion_file.read_text(encoding="utf-8"))
        self.assertEqual(assertion_artifact["assertion"], {"mode": "observation_only"})
        self.assertEqual(assertion_artifact["document_evidence"]["path"], evidence_file)
        sidecar = json.loads((ROOT / "reports/operation-document-evidence/v2/15158559-69640.json").read_text(encoding="utf-8"))
        self.assertEqual(sidecar["effect"]["status"], "unknown")
        self.assertIsNone(sidecar["effect"]["classification"])
        self.assertEqual(sidecar["response_contract"]["success_branches"][0]["schema_shape"]["status"], "incomplete")

        # This profile must be applied through its exact method, effect, auth,
        # and optional-input gates across the full denominator, not via a
        # hand-maintained operation allowlist.
        expected_profile_ids = {
            "0004cea4696c10954bc7db92690058078e6dd3a7ffddd92c61e7f4061e83ef0a",
            "002fc82b62ee5ef4c7bee1d5adabaddfc8ee3a0bc177093430e860ee588cae0f",
            "0056feae6364d6c9ac1847df845f3ac914038945f596c6782c011c1b99543ea7",
            "00d3c1379f31537f7b2fdc5e9532c7a80b2d372afadc2c2efe928f72926e7faa",
            "01b21791ff8f125fc023c74d53a27f8e0105d98c215bcf5675983090dd96db4e",
            "0209e26aca2704f993251f3798606a9bf167c07849efc56e7ec490a35f5d2700",
            "0294826332c0b8bad67b975db24d721753d1e375f7396d1b28a86818b1fcdc04",
            "02e2c621078d023e747971cac1ed800e169d056c1c74a8e5392a95249b7170f7",
            "045a909c56d3852db8198cf6196d2f89b7672bb8f1871b173b3071108f79dc92",
            "0461acf23da58c91d4f278b4285d058b8671da4f56809f9a854320aa0dfe2c25",
            "0465843e13ccd0ac0a809aca5c23ee40d1f0c6504902ca966e65ec7bddce1fe1",
            "04d1170f65e3e7f6f36ba0e84490f9825ba8ee51f543fb48734142d6f0c858dc",
            "05192c4ae28526fa5472ae7326785dfbd0c2ad49b77e951607562c9fa9af6e33",
            "0527dbed99c86d1a86ab5707a2f90706408841ac854e16ece3067236df4ebb3f",
            # These four source records establish retrieval in a documented
            # title/summary; their purpose facts are either neutral or absent.
            "046a332e30bf77079f1b24e5bee21f8a4c81d08ed0891fc743a44c78a66e3e23",
            "047e4ec527acda40be0506de92a5b6a2ef80b1033ab3702d7ba2dc96a7aab610",
            "05399b545b946ecd4ed43a729e6316b1a0070feceee9928c34e04824008ac77e",
            "057bc3dfac1acf01bee81a0d410f63e7d1cac42d691c4f6f33471bcae46a0854",
        }
        profile_records = {
            row["operation_identity"]["operation_id"]: row
            for row in records
            if row["request_plan"]["status"] == "complete"
            and row["operation_identity"]["operation_id"] != operation_id
        }
        self.assertEqual(set(profile_records), expected_profile_ids)
        for profiled_id, profiled_record in profile_records.items():
            self.assertEqual(profiled_record["runtime_binding"]["status"], "unbound", profiled_id)
            self.assertEqual(profiled_record["admission"]["status"], "not_admitted", profiled_id)
            profiled_contract = profiled_record["request_plan"]["request_contract"]
            self.assertEqual(profiled_contract["response_assertion"]["kind"], "observation_only", profiled_id)
            self.assertEqual(profiled_contract["response_assertion"]["empty_result_semantics"], "not_applicable", profiled_id)
            self.assertEqual([item["name"] for item in profiled_contract["parameters"]], ["serviceKey"], profiled_id)
            self.assertEqual(profiled_contract["parameters"][0]["value_strategy"]["kind"], "credential_reference", profiled_id)
            self.assertEqual(profiled_contract["limits"]["request_budget"], 1, profiled_id)
            assertion_path = f"reports/operation-response-assertions/{profiled_id}.json"
            assertion_artifact = json.loads(outputs[assertion_path])
            self.assertEqual(assertion_artifact["assertion"], {"mode": "observation_only"}, profiled_id)
            self.assertEqual(assertion_artifact["operation_identity"]["operation_id"], profiled_id)
        self.assertEqual(index["summary"]["admitted"], 0)

    def test_v2_normalizer_preserves_unknown_collection_and_separates_http_error_branches(self):
        document = json.loads((ROOT / "reports/operation-document-evidence/v2/15158559-69640.json").read_text(encoding="utf-8"))
        normalized = self.compiler.normalize_operation_document_evidence_v2(document)
        self.assertEqual(normalized["source_parse_status"], document["parse_status"])
        self.assertEqual(len(normalized["response"]["success_branches"]), 1)
        self.assertEqual(normalized["response"]["http_error_branches"], [])
        self.assertEqual(normalized["response"]["success_branches"][0]["classification"], "success")
        self.assertEqual(normalized["response"]["success_branches"][0]["schema_shape_status"], "incomplete")
        self.assertEqual(normalized["response"]["success_branches"][0]["coded_result_inventory_status"], "incomplete")
        self.assertEqual(normalized["response"]["result_collection"]["status"], "unknown")
        self.assertIsInstance(normalized["response"]["result_collection"], dict)

        synthetic = json.loads(json.dumps(document))
        error_branch = json.loads(json.dumps(synthetic["response_contract"]["success_branches"][0]))
        error_branch["http_status_code"] = 429
        synthetic["response_contract"]["documented_http_error_branches"].append(error_branch)
        branch_union = self.compiler.normalize_operation_document_evidence_v2(synthetic)["response"]
        self.assertEqual([branch["http_status_code"] for branch in branch_union["success_branches"]], [200])
        self.assertEqual([branch["http_status_code"] for branch in branch_union["http_error_branches"]], [429])
        self.assertEqual(branch_union["http_error_branches"][0]["classification"], "provider_error")
        self.assertNotEqual(branch_union["success_branches"][0]["source"], branch_union["http_error_branches"][0]["source"])

    def test_identity_set_digest_uses_sorted_canonical_json_array(self):
        ids = ["operation-z", "operation-a", "operation-m"]
        expected = self.compiler.sha256(b'["operation-a","operation-m","operation-z"]')
        self.assertEqual(self.compiler.identity_set_digest(ids), expected)

    def test_consumer_schema_hashes_match_reviewed_abi_checkpoints(self):
        evidence_schema = ROOT / "schemas/datapan.operation-document-evidence.v1.schema.json"
        self.assertEqual(
            self.compiler.sha256(SCHEMA_PATH.read_bytes()),
            "cafa93014d7a32ef072f74df1a730f681e5b206440e4a83e9cdf426f6686e162",
        )
        policy_schema = ROOT / "schemas/datapan.operation-observation-policy.v1.schema.json"
        self.assertEqual(
            self.compiler.sha256(policy_schema.read_bytes()),
            "acd9e80d3f41e4a0f16b010975fc716bf1ead5c5a3b128c12d03dd51b025f31d",
        )
        self.assertEqual(
            self.compiler.sha256(evidence_schema.read_bytes()),
            "0b4a5a7ab10eeccb523d2af8a8e62e76f14a6243eea00558ac49e9959e7a3d1d",
        )
        assertion_schema = ROOT / "schemas/datapan.operation-response-assertion.v2.schema.json"
        self.assertEqual(
            self.compiler.sha256(assertion_schema.read_bytes()),
            "78878ab22183e419e58d2a15b0a6a32bc585a3822cfa5a3a4bfd9f23d893055b",
        )
        evidence_v2_schema = ROOT / "schemas/datapan.operation-document-evidence.v2.schema.json"
        self.assertEqual(
            self.compiler.sha256(evidence_v2_schema.read_bytes()),
            "d6edb7dad63b9d7cdac6753fc02cba962cb8d96d7c01119c031935abfc973108",
        )

    def test_previous_plan_schema_rejects_observation_only_kind(self):
        previous = subprocess.run(
            ["git", "-C", str(ROOT), "show", "b0ff9e7cb3ec5cdcecb35a8fc416123a525b286d:schemas/datapan.operation-observation-plan.v1.schema.json"],
            check=True,
            capture_output=True,
            text=True,
        )
        old_schema = json.loads(previous.stdout)
        assertion = {
            "kind": "observation_only",
            "assertion_ref": "reports/observation.json#/assertion",
            "empty_result_semantics": "not_applicable",
            "evidence_refs": [{"artifact_path": "policy.json", "sha256": "a" * 64, "json_pointer": "#/assertion", "evidence_kind": "reviewed_policy"}],
        }
        old_assertion_schema = self.compiler.json_pointer_value(
            old_schema, "#/$defs/request_contract/properties/response_assertion"
        )
        with self.assertRaises(jsonschema.ValidationError):
            jsonschema.Draft202012Validator(old_assertion_schema).validate(assertion)

    def test_source_revision_rejects_changed_legacy_and_provider_index_inputs(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            paths = [
                "scripts/generate-operation-observation-plan.py",
                "scripts/operation_document_evidence.py",
                "schemas/datapan.operation-observation-plan.v1.schema.json",
                "schemas/datapan.operation-document-evidence.v1.schema.json",
                "schemas/datapan.operation-observation-policy.v1.schema.json",
                "schemas/datapan.operation-response-assertion.v2.schema.json",
                "reports/data-go-kr/operation-manifest.json",
                "reports/data-go-kr/operation-document.json",
                "reports/ecos/operation-denominator.json",
                "policy/health-probe-canaries.json",
                "policy/operation-observation-policies.v1.json",
                "data/provider-index.json",
            ]
            for index, relative in enumerate(paths):
                path = root / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(f"pinned input {index}\n", encoding="utf-8")
            subprocess.run(["git", "-C", str(root), "init", "-q"], check=True)
            subprocess.run(["git", "-C", str(root), "config", "user.email", "test@example.invalid"], check=True)
            subprocess.run(["git", "-C", str(root), "config", "user.name", "Test"], check=True)
            subprocess.run(["git", "-C", str(root), "add", "."], check=True)
            subprocess.run(["git", "-C", str(root), "commit", "-qm", "pinned source fixture"], check=True)
            revision = subprocess.run(
                ["git", "-C", str(root), "rev-parse", "HEAD"], check=True, capture_output=True, text=True
            ).stdout.strip()
            refs = [{"path": relative} for relative in paths]
            by_path = {ref["path"]: ref for ref in refs}
            index = {
                "registry_revision": revision,
                "generation_inputs": {
                    "generator_path": paths[0],
                    "operation_manifest": by_path["reports/data-go-kr/operation-manifest.json"],
                    "operation_denominators": [by_path["reports/ecos/operation-denominator.json"]],
                    "legacy_policy": by_path["policy/health-probe-canaries.json"],
                    "provider_index": by_path["data/provider-index.json"],
                    "document_evidence": [by_path["reports/data-go-kr/operation-document.json"]],
                },
                "source_scopes": [{"source_artifacts": refs}],
            }
            self.compiler.verify_source_revision(index, root)
            for relative in (
                "policy/health-probe-canaries.json",
                "data/provider-index.json",
                "policy/operation-observation-policies.v1.json",
                "schemas/datapan.operation-observation-policy.v1.schema.json",
                "schemas/datapan.operation-response-assertion.v2.schema.json",
            ):
                current = root / relative
                current.write_text("changed after pinned source revision\n", encoding="utf-8")
                with self.assertRaisesRegex(self.compiler.PlanError, "release tree differs from pinned source commit input"):
                    self.compiler.verify_source_revision(index, root)
                current.write_text(subprocess.run(
                    ["git", "-C", str(root), "show", f"{revision}:{relative}"], check=True, capture_output=True
                ).stdout.decode("utf-8"), encoding="utf-8")

    def test_source_revision_ancestor_check_works_after_bounded_shallow_checkout_deepen(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            origin = root / "origin"
            bare = root / "origin.git"
            checkout = root / "ci-checkout"
            origin.mkdir()
            paths = {
                "scripts/generator.py": "pinned generator\n",
                "schemas/datapan.operation-observation-plan.v1.schema.json": "pinned schema\n",
                "manifest.json": "pinned manifest\n",
                "denominators/operations.json": "pinned denominator\n",
                "policy/legacy.json": "pinned policy\n",
                "data/provider-index.json": "pinned provider index\n",
            }
            for relative, value in paths.items():
                path = origin / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(value, encoding="utf-8")
            subprocess.run(["git", "-C", str(origin), "init", "-q", "--initial-branch=main"], check=True)
            subprocess.run(["git", "-C", str(origin), "config", "user.email", "test@example.invalid"], check=True)
            subprocess.run(["git", "-C", str(origin), "config", "user.name", "Test"], check=True)

            def commit(message: str) -> str:
                subprocess.run(["git", "-C", str(origin), "add", "."], check=True)
                subprocess.run(["git", "-C", str(origin), "commit", "-qm", message], check=True)
                return subprocess.run(["git", "-C", str(origin), "rev-parse", "HEAD"], check=True, capture_output=True, text=True).stdout.strip()

            source_revision = commit("pinned operation-plan source")
            for commit_index in range(5):
                (origin / "progress.txt").write_text(f"release commit {commit_index}\n", encoding="utf-8")
                release_head = commit(f"release update {commit_index}")
            subprocess.run(["git", "clone", "-q", "--bare", str(origin), str(bare)], check=True)
            subprocess.run(["git", "clone", "-q", "--depth=1", "--branch", "main", f"file://{bare}", str(checkout)], check=True)
            subprocess.run(["git", "-C", str(checkout), "fetch", "--no-tags", "--depth=1", "origin", source_revision], check=True)

            by_path = {relative: {"path": relative} for relative in paths}
            index = {
                "registry_revision": source_revision,
                "generation_inputs": {
                    "generator_path": "scripts/generator.py",
                    "operation_manifest": by_path["manifest.json"],
                    "operation_denominators": [by_path["denominators/operations.json"]],
                    "legacy_policy": by_path["policy/legacy.json"],
                    "provider_index": by_path["data/provider-index.json"],
                    "document_evidence": [],
                },
                "source_scopes": [{"source_artifacts": []}],
            }
            with self.assertRaisesRegex(self.compiler.PlanError, "not an ancestor"):
                self.compiler.verify_source_revision(index, checkout)

            subprocess.run(["git", "-C", str(checkout), "fetch", "--no-tags", "--depth=128", "origin", release_head], check=True)
            self.compiler.verify_source_revision(index, checkout)

    def test_partial_source_denominator_must_match_profile_and_candidate_ids(self):
        profile = {"source_id": "ecos", "provider": "ECOS", "adapter": {"name": "ecos", "status": "registered"}}
        self.assertEqual(self.compiler.checked_adapter_id("ecos", "ECOS", profile), "ecos")
        with self.assertRaisesRegex(self.compiler.PlanError, "source profile provider mismatch"):
            self.compiler.checked_adapter_id("ecos", "OTHER", profile)

        denominator = {"operations": [{"operation_id": "ecos-statistic-search-102y004", "method": "GET", "endpoint_template": "https://ecos.example.test/StatisticSearch"}]}
        catalog = {
            "source_id": "ecos",
            "provider": "ECOS",
            "source_profile": "sources/ecos.json",
            "summary": {"candidates": 1},
            "candidates": [{"candidate_id": "ecos-statistic-search-102y004", "method": "GET", "endpoint_template": "https://ecos.example.test/StatisticSearch"}],
        }
        self.compiler.check_partial_catalog_identity_set("ecos", "ECOS", "sources/ecos.json", denominator, catalog)
        mismatched = copy.deepcopy(denominator)
        mismatched["operations"][0]["operation_id"] = "different-id"
        with self.assertRaisesRegex(self.compiler.PlanError, "identity set"):
            self.compiler.check_partial_catalog_identity_set("ecos", "ECOS", "sources/ecos.json", mismatched, catalog)
        endpoint_mismatch = copy.deepcopy(denominator)
        endpoint_mismatch["operations"][0]["endpoint_template"] = "https://ecos.example.test/other"
        with self.assertRaisesRegex(self.compiler.PlanError, "endpoint differs"):
            self.compiler.check_partial_catalog_identity_set("ecos", "ECOS", "sources/ecos.json", endpoint_mismatch, catalog)

    def test_kosis_known_operation_uses_the_official_table_selection_endpoint(self):
        denominator = json.loads((ROOT / "reports/kosis/operation-denominator.json").read_text(encoding="utf-8"))
        profile = json.loads((ROOT / "sources/kosis.json").read_text(encoding="utf-8"))
        catalog = json.loads((ROOT / "reports/kosis/runtime-candidates.json").read_text(encoding="utf-8"))
        operation = denominator["operations"][0]
        candidate = catalog["candidates"][0]
        official_endpoint = "https://kosis.kr/openapi/Param/statisticsParameterData.do?method=getList"

        self.assertEqual(denominator["summary"]["operations"], 1)
        self.assertEqual(operation["operation_id"], "kosis-statistics-data-dt-1b41")
        self.assertEqual(operation["endpoint_template"], official_endpoint)
        self.assertEqual(candidate["candidate_id"], operation["operation_id"])
        self.assertEqual(candidate["endpoint_template"], official_endpoint)
        self.assertEqual(profile["catalogue"]["detail_endpoint"], official_endpoint)
        self.assertEqual(profile["references"]["api_docs_url"], "https://kosis.kr/openapi/devGuide/devGuide_0201List.do")

        denominator_ref = self.compiler.artifact_ref(ROOT / "reports/kosis/operation-denominator.json")
        scope = {
            "source_id": "kosis",
            "provider": "KOSIS",
            "adapter_id": "kosis",
            "inventory_status": "partial",
            "inventory_unknown": True,
            "test_only": False,
            "source_artifacts": [denominator_ref],
        }
        plan = self.compiler.make_incomplete_plan(
            scope=scope,
            operation_id=operation["operation_id"],
            protocol="HTTP",
            identity={"registered_endpoint": self.compiler.registered_endpoint(operation["endpoint_template"])},
            evidence={
                "artifact_path": denominator_ref["path"],
                "sha256": denominator_ref["sha256"],
                "json_pointer": "#/operations/0",
                "evidence_kind": "operation_denominator",
            },
        )
        self.assertTrue(plan["source_binding"]["inventory_unknown"])
        self.assertEqual(plan["operation_identity"]["operation_id"], operation["operation_id"])
        self.assertEqual(plan["request_plan"]["status"], "incomplete")
        self.validator.validate(plan)

    def test_documented_read_only_effect_closes_only_that_request_plan_gap(self):
        scope = {
            "source_id": "data_go_kr",
            "provider": "data.go.kr",
            "adapter_id": "data-go-kr",
            "inventory_status": "source_complete",
            "inventory_unknown": False,
            "test_only": False,
            "source_artifacts": [{"path": "manifest.json", "sha256": "a" * 64, "bytes": 1}],
        }
        plan = self.compiler.make_incomplete_plan(
            scope=scope,
            operation_id="operation-example",
            protocol="REST",
            identity={"dataset_id": "15001697", "operation_name": "목록 조회", "upstream_operation_key": "24807"},
            evidence={
                "artifact_path": "reports/data-go-kr/operation-manifest.json",
                "sha256": "b" * 64,
                "json_pointer": "#/operations/0",
                "evidence_kind": "operation_manifest",
            },
        )
        document = {
            "identity": {"protocol": "REST"},
            "effect": {
                "classification": "read_only",
                "status": "documented",
                "authority": "operation_document",
                "source_refs": [{"evidence_kind": "operation_effect", "locator": {"source_id": "guide"}}],
            },
            "transport": {"http_method": {"value": None, "status": "unknown", "authority_scope": "service_level_only"}},
            "parameters": [],
            "response_assertion": {
                "kind": "unknown",
                "fields": [],
                "empty_result_semantics": {"value": None, "status": "unknown", "source_refs": []},
            },
        }
        self.compiler._resolve_documented_missing_fields(plan, document)
        missing = plan["request_plan"]["missing_fields"]
        self.assertNotIn("operation_effect_read_only_authority", missing)
        self.assertIn("operation_method_authority", missing)
        self.assertIn("safe_value_strategy_for_required_inputs", missing)
        self.assertEqual(plan["request_plan"]["status"], "incomplete")
        self.assertNotIn("request_contract", plan["request_plan"])

    def test_reviewed_effect_uses_either_grounded_operation_text_and_screens_all_text(self):
        selector = {
            "source_id": "data_go_kr",
            "provider": "data.go.kr",
            "protocol": "REST",
            "method": "GET",
        }
        effect_review = {
            "classification": "read_only",
            "basis": "rfc9110_safe_method_and_retrieval_purpose",
            "rfc_reference": "https://www.rfc-editor.org/rfc/rfc9110#section-9.2.1",
            "purpose_terms": ["조회", "목록"],
        }

        def source_fact(value, evidence_kind, status="documented"):
            return {
                "status": status,
                "value": value,
                "source_refs": [{"evidence_kind": evidence_kind}] if status == "documented" else [],
            }

        def document(title, purpose):
            return {
                "identity": {
                    "source_id": "data_go_kr",
                    "provider": "data.go.kr",
                    "protocol": "REST",
                    "operation_id": "operation-example",
                },
                "effect": {"status": "unknown", "classification": None, "source_refs": []},
                "transport": {
                    "http_method": {
                        "status": "documented",
                        "authority_scope": "operation_specific",
                        "value": "GET",
                        "source_refs": [{"evidence_kind": "operation_http_method"}],
                    },
                    "fixed_query_selectors": [],
                },
                "operation_document": {"title": title, "purpose": purpose},
                "parameters": [],
            }

        title_only = document(
            source_fact("시설 목록 조회", "official_operation_title"),
            source_fact(None, "official_operation_purpose", status="not_found_in_parsed_operation_sources"),
        )
        self.assertTrue(
            self.compiler.reviewed_read_only_effect_matches(
                selector, effect_review, "data_go_kr", "data.go.kr",
                {"operation_id": "operation-example", "protocol": "REST"}, title_only,
            )
        )
        title_refs = self.compiler.reviewed_operation_text_refs(
            {"path": "reports/example.json", "sha256": "a" * 64}, title_only,
        )
        self.assertEqual([ref["json_pointer"] for ref in title_refs], ["#/operation_document/title"])

        purpose_only = document(
            source_fact("시설 정보", "official_operation_title", status="registered_manifest"),
            source_fact("시설의 목록을 조회", "official_operation_purpose"),
        )
        self.assertTrue(
            self.compiler.reviewed_read_only_effect_matches(
                selector, effect_review, "data_go_kr", "data.go.kr",
                {"operation_id": "operation-example", "protocol": "REST"}, purpose_only,
            )
        )
        purpose_refs = self.compiler.reviewed_operation_text_refs(
            {"path": "reports/example.json", "sha256": "a" * 64}, purpose_only,
        )
        self.assertEqual([ref["json_pointer"] for ref in purpose_refs], ["#/operation_document/purpose"])

        conflicting_text = document(
            source_fact("시설 정보 조회", "official_operation_title"),
            source_fact("시설 정보를 삭제", "official_operation_purpose"),
        )
        self.assertFalse(
            self.compiler.reviewed_read_only_effect_matches(
                selector, effect_review, "data_go_kr", "data.go.kr",
                {"operation_id": "operation-example", "protocol": "REST"}, conflicting_text,
            )
        )

        ungrounded_text = document(
            source_fact("시설 목록 조회", "official_operation_title", status="unknown"),
            source_fact(None, "official_operation_purpose", status="not_found_in_parsed_operation_sources"),
        )
        self.assertFalse(
            self.compiler.reviewed_read_only_effect_matches(
                selector, effect_review, "data_go_kr", "data.go.kr",
                {"operation_id": "operation-example", "protocol": "REST"}, ungrounded_text,
            )
        )

    def test_manifest_arithmetic_rejects_negative_counts_duplicate_ids_and_unknown_protocol(self):
        manifest = json.loads((ROOT / "reports/data-go-kr/operation-manifest.json").read_text(encoding="utf-8"))
        negative = copy.deepcopy(manifest)
        negative["summary"]["protocols"]["REST"] = -1
        with self.assertRaises(self.compiler.PlanError):
            self.compiler.check_data_go_manifest(negative)

        duplicate = copy.deepcopy(manifest)
        duplicate["operations"][1]["operation_id"] = duplicate["operations"][0]["operation_id"]
        with self.assertRaises(self.compiler.PlanError):
            self.compiler.check_data_go_manifest(duplicate)

        unknown_protocol = copy.deepcopy(manifest)
        unknown_protocol["operations"][0]["protocol"] = "LINK"
        with self.assertRaises(self.compiler.PlanError):
            self.compiler.check_data_go_manifest(unknown_protocol)

        unknown_exclusion = copy.deepcopy(manifest)
        unknown_exclusion["summary"]["exclusions"]["mystery"] = 0
        with self.assertRaisesRegex(self.compiler.PlanError, "exactly the supported exclusion counters"):
            self.compiler.check_data_go_manifest(unknown_exclusion)

        missing_identity = copy.deepcopy(manifest)
        del missing_identity["operations"][0]["provenance"]["upstream_operation_key"]
        with self.assertRaisesRegex(self.compiler.PlanError, "lacks upstream_operation_key"):
            self.compiler.check_data_go_manifest(missing_identity)

    def test_synthetic_contract_rejects_casefold_duplicates_and_evidence_mismatch(self):
        fixture = json.loads((ROOT / "fixtures/operation-observation-plan/synthetic-rest-list.json").read_text(encoding="utf-8"))
        duplicate = copy.deepcopy(fixture)
        duplicate["request_plan"]["request_contract"]["parameters"].append(
            copy.deepcopy(duplicate["request_plan"]["request_contract"]["parameters"][0])
        )
        duplicate["request_plan"]["request_contract"]["parameters"][1]["name"] = "PAGE"
        with self.assertRaisesRegex(self.compiler.PlanError, "case-insensitively"):
            self.compiler.validate_record(duplicate, self.schema, ROOT)

        mismatch = copy.deepcopy(fixture)
        mismatch["request_plan"]["request_contract"]["transport"]["http_method"] = "POST"
        with self.assertRaisesRegex(self.compiler.PlanError, "transport evidence mismatch"):
            self.compiler.validate_record(mismatch, self.schema, ROOT)

    def test_release_manifest_binds_index_and_every_bounded_shard(self):
        manifest = json.loads((ROOT / "manifest.json").read_text(encoding="utf-8"))
        artifact_paths = {item["path"]: item for item in manifest["artifacts"]}
        index_path = "reports/operation-observation-plan/index.json"
        if index_path not in artifact_paths:
            self.skipTest("release manifest registration is generated after the compiler output")
        self.assertEqual(artifact_paths[index_path]["kind"], "operation_observation_plan")
        self.assertEqual(artifact_paths[index_path]["schema"], self.schema["$id"])
        index = json.loads((ROOT / index_path).read_text(encoding="utf-8"))
        shard_paths = {entry["path"] for entry in index["shards"]}
        self.assertTrue(shard_paths)
        for path in shard_paths:
            self.assertEqual(artifact_paths[path]["kind"], "operation_observation_plan_shard")
            self.assertEqual(artifact_paths[path]["schema"], self.schema["$id"])
        index = json.loads((ROOT / index_path).read_text(encoding="utf-8"))
        indexed_document_artifacts = {row["path"] for row in index["generation_inputs"]["document_evidence"]}
        indexed_document_artifacts.update(
            ref["path"]
            for scope in index["source_scopes"]
            for ref in scope["source_artifacts"]
            if ref["path"].startswith("reports/operation-document-evidence/")
            and artifact_paths.get(ref["path"], {}).get("kind") == "operation_document_evidence"
        )
        self.assertEqual(
            {path for path, row in artifact_paths.items() if row["kind"] == "operation_document_evidence"},
            indexed_document_artifacts,
        )
        for path, row in artifact_paths.items():
            if row["kind"] == "operation_document_evidence":
                if "/receipts/" in path:
                    evidence_schema_path = "schemas/datapan.operation-document-capture-receipt.v2.schema.json"
                elif "/v2/" in path or "/source-scopes/" in path:
                    evidence_schema_path = "schemas/datapan.operation-document-evidence.v2.schema.json"
                else:
                    evidence_schema_path = "schemas/datapan.operation-document-evidence.v1.schema.json"
                evidence_schema = json.loads((ROOT / evidence_schema_path).read_text(encoding="utf-8"))
                self.assertEqual(row["schema"], evidence_schema["$id"])


if __name__ == "__main__":
    unittest.main()
