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
        self.assertEqual(summary["request_plans_complete"], 1)
        self.assertEqual(summary["request_plans_incomplete"], 12665)
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
        self.assertEqual(len(document_inputs), 105)
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
            "f56ec01a26662e05092118497bdc4d1c24612c35db870358ffb2058e9127454f",
        )
        policy_schema = ROOT / "schemas/datapan.operation-observation-policy.v1.schema.json"
        self.assertEqual(
            self.compiler.sha256(policy_schema.read_bytes()),
            "16fa872c0e7d598e55d81814867566576f1962479627ac43eebd66d1d2a62d0f",
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
        evidence_schema = json.loads((ROOT / "schemas/datapan.operation-document-evidence.v1.schema.json").read_text(encoding="utf-8"))
        self.assertEqual(
            {path for path, row in artifact_paths.items() if row["kind"] == "operation_document_evidence"},
            {row["path"] for row in json.loads((ROOT / index_path).read_text(encoding="utf-8"))["generation_inputs"]["document_evidence"]},
        )
        for path, row in artifact_paths.items():
            if row["kind"] == "operation_document_evidence":
                self.assertEqual(row["schema"], evidence_schema["$id"])


if __name__ == "__main__":
    unittest.main()
