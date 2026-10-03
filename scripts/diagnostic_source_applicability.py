#!/usr/bin/env python3
"""Build and validate the current-source applicability receipt for the draft diagnostic contract."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import pathlib
import subprocess
from typing import Any

import jsonschema


ROOT = pathlib.Path(__file__).resolve().parents[1]
REPORT = ROOT / "reports/diagnostic-current-source-applicability.json"
SCHEMA = ROOT / "schemas/datapan.diagnostic-current-source-applicability.v1.schema.json"
MAPPING = ROOT / "drafts/diagnostic-envelope/data-go-kr-evidence-mapping.v1.json"
PROOF = ROOT / "drafts/diagnostic-envelope/data-go-kr-registry-identity-proof.v1.json"
ARCHIVE_DIR = ROOT / "tests/fixtures/diagnostic-source-applicability"
ARCHIVED_HEALTH = ARCHIVE_DIR / "health-probe-catalog.v1.json"
ARCHIVE_PROVENANCE = ARCHIVE_DIR / "health-probe-catalog.provenance.v1.json"

HISTORICAL_REGISTRY = {
    "path": "data/data-go-kr.registry.json",
    "bytes": 137735169,
    "sha256": "eeda72ee8590f458de8d75703662578e80edf3e61282f0e5e67547c4f6e5f644",
}
HISTORICAL_HEALTH = {
    "path": "reports/health-probe-catalog.json",
    "bytes": 16353,
    "sha256": "e84f0da2f532a32833def1118a4610bf2322f370783d120b84cf85306d244840",
    "git_commit": "b49d66b97d8155c34649f4dd2040b884c4212d64",
    "git_blob": "f3c286b8b8998e2d139a3d71e02dee5af032856f",
}

# These immutable bytes are the historical consumer and human-review contract.
# They are intentionally separate from current-source identities below.
HISTORICAL_INPUTS = [
    ("drafts/diagnostic-envelope/data-go-kr-evidence-mapping.v1.json", 19525, "da55d52d2ee1f197969ac63a1d5ab5b98e3b88fd65f90d6a48800d2e3c522d33"),
    ("drafts/diagnostic-envelope/consumer-contract.v1.json", 6125, "02146e5cbc84a4f7e9b6883ff049c62b7f188cccd731c300e762395b486483a5"),
    ("drafts/diagnostic-envelope/datapan.diagnostic-envelope.v1.schema.json", 45525, "da254b40947462347fcda90fdd7686b6632c76943b438f2046a28f079f33e403"),
    ("drafts/diagnostic-envelope/data-go-kr-registry-identity-proof.v1.json", 4364, "b4b8fac3de722db5cf3a55ad195be90c8b57a16f11791213542fd67cbc8c4df0"),
    ("policy/data-go-kr-diagnostic-evidence-mapping.v1.json", 27117, "c79d9a5afe2702b22cf49fff97178b4f79ffc16eaa4624b68f826fb8c9509273"),
    ("drafts/diagnostic-envelope/release-candidate/consumer-proof-intake.v1.json", 3539, "8291c9958c470dbf85eea72e99d180c2e745a81667c078a2f9c12fed16d96bf8"),
    ("drafts/diagnostic-envelope/consumer-compatibility/datapan-cli.v1.json", 2099, "b1487e736c8b853ae950e70fa8845754ae5722c9dc3d215d50b3a8e767f38068"),
    ("drafts/diagnostic-envelope/consumer-compatibility/datapan-health.v1.json", 2170, "e831df46e50107c116132f423525af5b1ea8c9743c014956a2fc3732077db70c"),
    ("drafts/diagnostic-envelope/consumer-compatibility/datapan-web.v1.json", 2136, "6dcdb0b7404a43e0a58fbd1b2cbd43ebeded4778d67f1cb3f85015f391cd673c"),
    ("drafts/diagnostic-envelope/release-candidate/proofs/datapan-cli.compatibility-receipt.v1.json", 5149, "b2256d7030bacaa640d964439de6292042d3821e0ce756187856f1d3f547220b"),
    ("drafts/diagnostic-envelope/release-candidate/proofs/datapan-health.compatibility-receipt.v1.json", 7968, "23fe6cf7fbb63268dab889ed516a494a915877f15664821f445656fae36b18e8"),
    ("drafts/diagnostic-envelope/release-candidate/proofs/datapan-web.compatibility-receipt.v1.json", 5182, "b032daa09fbb8d01508dac7ad76ce55754ba977492972c85917906468e86d180"),
    ("drafts/diagnostic-envelope/fixtures/approval-propagating.json", 3004, "fe000f4082f948d6a96f045d7fae91c6bdf7288c6746196a8c7b0868d6416099"),
    ("drafts/diagnostic-envelope/fixtures/approval-required.json", 2247, "d11fc4e18aee6fe1a7f5c9c0a94a1e6e1bae0177447f2f6bcc0dddbe6961e7d3"),
    ("drafts/diagnostic-envelope/fixtures/contract-drift.json", 2205, "13bc8af8c6b1540ef91a49f60ed9aab5514fabee3316868dc9f946adbe1da470"),
    ("drafts/diagnostic-envelope/fixtures/credential-invalid.json", 2164, "c5796d7bf59c6f282f9f75b14717a51ff716859e06f6325e784c48d507816497"),
    ("drafts/diagnostic-envelope/fixtures/invalid-input.json", 2209, "80adb4fce6ede5c34223468bf26b69e90c20c92d74661777c458a5238ad6ab07"),
    ("drafts/diagnostic-envelope/fixtures/provider-outage.json", 3588, "33c0160c4cf136b34dc3befa1ff5803c71f3c37d7946fa2b56e70c69d4be6200"),
    ("drafts/diagnostic-envelope/fixtures/rate-limited.json", 2108, "8be4fb69e91ae42c2a03510458b9b9fd23cf4780d551ea1ddaa505c8bc40d318"),
    ("drafts/diagnostic-envelope/fixtures/ready.json", 1967, "7ae2306176bdd007ca2a4ca822240e4e515c76967b2af0c816584463aee420fc"),
    ("drafts/diagnostic-envelope/fixtures/semantic-quality.json", 2966, "06fcc308aa039861b38f6da2fee8f23150ea7d2cac998b5d9e05b011ca1ca9b0"),
    ("drafts/diagnostic-envelope/fixtures/stale-data.json", 2465, "f4cc2d0f34bdfb74bbed9a3bcb5cad7b0f5444fa5ded6ca1742c916799aecf92"),
    ("drafts/diagnostic-envelope/fixtures/unknown.json", 2035, "e0635cf4980438141007607c66eca821383f605393a69bdb03522b3873c1dcf0"),
]
EXPECTED_CURRENT_AUTHORITATIVE_PATHS = {
    "reports/data-go-kr/error-action-catalog.json",
    "reports/error-action-routing-rollup.json",
    "reports/health-probe-catalog.json",
    "sources/data_go_kr.json",
    "schemas/datapan.source-profile.v1.schema.json",
    "data/data-go-kr.registry.json",
}


def sha256_file(path: pathlib.Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def artifact_identity(path: pathlib.Path, relative_path: str | None = None) -> dict[str, Any]:
    if not path.is_file():
        raise ValueError(f"required input is missing: {path}")
    return {
        "path": relative_path or path.relative_to(ROOT).as_posix(),
        "bytes": path.stat().st_size,
        "sha256": sha256_file(path),
    }


def load_json(path: pathlib.Path) -> Any:
    with path.open(encoding="utf-8") as handle:
        return json.load(handle)


def canonical_digest(value: Any) -> str:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def verify_archive_provenance() -> dict[str, Any]:
    provenance = load_json(ARCHIVE_PROVENANCE)
    expected = {
        "schema_version": "datapan.diagnostic-historical-source-provenance.v1",
        "path": HISTORICAL_HEALTH["path"],
        "git_commit": HISTORICAL_HEALTH["git_commit"],
        "git_blob": HISTORICAL_HEALTH["git_blob"],
        "bytes": HISTORICAL_HEALTH["bytes"],
        "sha256": HISTORICAL_HEALTH["sha256"],
    }
    if provenance != expected:
        raise ValueError("historical health source provenance descriptor drift")
    archive = ARCHIVED_HEALTH.read_bytes()
    if len(archive) != HISTORICAL_HEALTH["bytes"] or hashlib.sha256(archive).hexdigest() != HISTORICAL_HEALTH["sha256"]:
        raise ValueError("historical health archive digest drift")
    git_blob = hashlib.sha1(b"blob " + str(len(archive)).encode("ascii") + b"\0" + archive).hexdigest()
    if git_blob != HISTORICAL_HEALTH["git_blob"]:
        raise ValueError("historical health archive Git blob drift")
    try:
        commit_blob = subprocess.check_output(
            ["git", "rev-parse", f"{HISTORICAL_HEALTH['git_commit']}:{HISTORICAL_HEALTH['path']}"],
            cwd=ROOT,
            stderr=subprocess.STDOUT,
            text=True,
        ).strip()
    except subprocess.CalledProcessError as exc:
        raise ValueError("historical health Git commit is unavailable; fetch the pinned commit before validation") from exc
    if commit_blob != HISTORICAL_HEALTH["git_blob"]:
        raise ValueError("historical health Git commit does not contain the pinned blob")
    return {
        "artifact": artifact_identity(ARCHIVED_HEALTH),
        "source_git": {
            "path": HISTORICAL_HEALTH["path"],
            "commit": HISTORICAL_HEALTH["git_commit"],
            "blob": HISTORICAL_HEALTH["git_blob"],
        },
    }


def validate_historical_inputs() -> list[dict[str, Any]]:
    identities = []
    for path, expected_bytes, expected_sha256 in HISTORICAL_INPUTS:
        identity = artifact_identity(ROOT / path, path)
        if identity["bytes"] != expected_bytes or identity["sha256"] != expected_sha256:
            raise ValueError(f"historical diagnostic input drift: {path}")
        identities.append(identity)
    archive = verify_archive_provenance()
    identities.append(archive["artifact"])
    identities.append(artifact_identity(ARCHIVE_PROVENANCE))
    return identities


def pointer_get(value: Any, pointer: str) -> Any:
    if pointer == "":
        return value
    if not pointer.startswith("/"):
        raise ValueError(f"invalid JSON pointer: {pointer}")
    current = value
    for raw in pointer[1:].split("/"):
        token = raw.replace("~1", "/").replace("~0", "~")
        current = current[int(token)] if isinstance(current, list) else current[token]
    return current


def expected_operation_identity(operation: dict[str, Any]) -> dict[str, Any]:
    return {
        "operation_name": operation["operation_name"],
        "source_operation_seq": operation["source_operation_seq"],
        "source_system": operation["source_system"],
        "source_url": operation["source_url"],
        "source_sha256": operation["source_sha256"],
    }


def current_operation_identities(dataset: Any) -> tuple[list[dict[str, Any]], bool]:
    operations = dataset.get("operations") if isinstance(dataset, dict) else None
    if not isinstance(operations, list):
        return [], False
    identities = []
    valid = True
    for operation in operations:
        if not isinstance(operation, dict):
            valid = False
            continue
        source = operation.get("source")
        if not isinstance(source, dict) or source.get("system") != "data.go.kr":
            continue
        seq = source.get("raw", {}).get("operation_seq") if isinstance(source.get("raw"), dict) else None
        try:
            name = operation["name"]
            source_url = source["url"]
            source_digest = canonical_digest(source)
        except (KeyError, TypeError, ValueError):
            valid = False
            continue
        if not isinstance(name, str) or not isinstance(seq, str) or not isinstance(source_url, str):
            valid = False
        identities.append({
            "operation_name": name if isinstance(name, str) else None,
            "source_operation_seq": seq if isinstance(seq, str) else None,
            "source_system": source["system"],
            "source_url": source_url if isinstance(source_url, str) else None,
            "source_sha256": source_digest,
        })
    identities.sort(key=lambda item: (str(item["source_operation_seq"]), str(item["operation_name"])))
    return identities, valid


def evaluate_registry_identities(registry: Any, proof: dict[str, Any]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    mismatches: list[dict[str, Any]] = []
    results = []
    if not isinstance(registry, list):
        for expected_dataset in proof.get("datasets", []):
            expected_operations = [expected_operation_identity(item) for item in expected_dataset["operations"]]
            results.append({"dataset_id": expected_dataset["dataset_id"], "expected_sha256": expected_dataset["dataset_sha256"], "actual_sha256": None, "status": "malformed", "expected_operations": expected_operations, "actual_operations": [], "operation_checks": []})
        return results, [{"code": "current_registry_not_array", "path": "data/data-go-kr.registry.json", "expected": "array", "actual": type(registry).__name__}]
    expected_datasets = proof.get("datasets", [])
    for expected_dataset in expected_datasets:
        dataset_id = expected_dataset["dataset_id"]
        matches = [item for item in registry if isinstance(item, dict) and item.get("id") == dataset_id]
        expected_operations = [expected_operation_identity(item) for item in expected_dataset["operations"]]
        if not matches:
            results.append({"dataset_id": dataset_id, "expected_sha256": expected_dataset["dataset_sha256"], "actual_sha256": None, "status": "missing", "expected_operations": expected_operations, "actual_operations": [], "operation_checks": []})
            mismatches.append({"code": "referenced_dataset_missing", "path": f"data/data-go-kr.registry.json#/id={dataset_id}", "expected": dataset_id, "actual": None})
            continue
        if len(matches) != 1:
            results.append({"dataset_id": dataset_id, "expected_sha256": expected_dataset["dataset_sha256"], "actual_sha256": None, "status": "ambiguous", "expected_operations": expected_operations, "actual_operations": [], "operation_checks": []})
            mismatches.append({"code": "referenced_dataset_ambiguous", "path": f"data/data-go-kr.registry.json#/id={dataset_id}", "expected": 1, "actual": len(matches)})
            continue
        dataset = matches[0]
        actual_operations, operations_valid = current_operation_identities(dataset)
        try:
            actual_dataset_sha256 = canonical_digest(dataset)
        except (TypeError, ValueError):
            actual_dataset_sha256 = None
        operation_checks = []
        consumed = set()
        for expected in expected_operations:
            candidates = [index for index, actual in enumerate(actual_operations) if all(actual.get(key) == expected.get(key) for key in ("operation_name", "source_operation_seq", "source_url"))]
            if len(candidates) == 1:
                index = candidates[0]
                actual = actual_operations[index]
                consumed.add(index)
                check_status = "matched" if actual == expected else "mismatch"
            elif len(candidates) > 1:
                actual = None
                check_status = "ambiguous"
            else:
                actual = None
                check_status = "missing"
            operation_checks.append({"expected": expected, "actual": actual, "status": check_status})
            if check_status != "matched":
                mismatches.append({"code": f"referenced_operation_{check_status}", "path": f"data/data-go-kr.registry.json#/id={dataset_id}/operations", "expected": expected, "actual": actual})
        unmatched = [item for index, item in enumerate(actual_operations) if index not in consumed]
        dataset_matches = actual_dataset_sha256 == expected_dataset["dataset_sha256"] and operations_valid and all(item["status"] == "matched" for item in operation_checks) and not unmatched
        status = "matched" if dataset_matches else ("malformed" if not operations_valid or actual_dataset_sha256 is None else "mismatch")
        results.append({
            "dataset_id": dataset_id,
            "expected_sha256": expected_dataset["dataset_sha256"],
            "actual_sha256": actual_dataset_sha256,
            "status": status,
            "expected_operations": expected_operations,
            "actual_operations": actual_operations,
            "operation_checks": operation_checks,
            "unmatched_current_operations": unmatched,
        })
        if actual_dataset_sha256 != expected_dataset["dataset_sha256"]:
            mismatches.append({"code": "referenced_dataset_identity_changed", "path": f"data/data-go-kr.registry.json#/id={dataset_id}", "expected": expected_dataset["dataset_sha256"], "actual": actual_dataset_sha256})
        elif status == "malformed":
            mismatches.append({"code": "referenced_dataset_malformed", "path": f"data/data-go-kr.registry.json#/id={dataset_id}", "expected": "well-formed exact source identities", "actual": "malformed source operations"})
        if unmatched:
            mismatches.append({"code": "unreferenced_data_go_kr_operations", "path": f"data/data-go-kr.registry.json#/id={dataset_id}/operations", "expected": [], "actual": unmatched})
    return results, mismatches


def load_mapping_validator() -> Any:
    path = ROOT / "scripts/validate-diagnostic-evidence-mapping-draft.py"
    spec = importlib.util.spec_from_file_location("diagnostic_mapping_history", path)
    if spec is None or spec.loader is None:
        raise ValueError("cannot load historical diagnostic mapping validator")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def build_report(current_paths: dict[str, pathlib.Path] | None = None) -> dict[str, Any]:
    current_paths = current_paths or {}
    historical_inputs = validate_historical_inputs()
    mapping_validator = load_mapping_validator()
    mapping = mapping_validator.load(mapping_validator.MAPPING)
    proof = mapping_validator.load(PROOF)
    # Existing contract, mapping, redaction, fixtures, packets, and minimized proof
    # checks remain authoritative for the historical scope.
    mapping_validator.validate_all()

    authoritative = mapping["authoritative_inputs"]
    if {item["path"] for item in authoritative} != EXPECTED_CURRENT_AUTHORITATIVE_PATHS:
        raise ValueError("historical mapping authoritative input allowlist drift")
    current_inputs = []
    by_path = {item["path"]: item for item in authoritative}
    registry_relative = "data/data-go-kr.registry.json"
    health_relative = "reports/health-probe-catalog.json"
    registry_path = current_paths.get(registry_relative, ROOT / registry_relative)
    health_path = current_paths.get(health_relative, ROOT / health_relative)
    current_registry = artifact_identity(registry_path, registry_relative)
    current_health = artifact_identity(health_path, health_relative)
    with registry_path.open("rb") as handle:
        registry_prefix = handle.read(160)
    if registry_prefix.startswith(b"version https://git-lfs.github.com/spec/v1"):
        raise ValueError("current registry is an unmaterialized Git LFS pointer; materialize exact candidate bytes first")
    mismatches: list[dict[str, Any]] = []
    for path, historical in ((registry_path, HISTORICAL_REGISTRY), (health_path, HISTORICAL_HEALTH)):
        current = current_registry if path == registry_path else current_health
        if current["bytes"] != historical["bytes"] or current["sha256"] != historical["sha256"]:
            mismatches.append({"code": "current_source_bytes_changed", "path": historical["path"], "expected": {"bytes": historical["bytes"], "sha256": historical["sha256"]}, "actual": {"bytes": current["bytes"], "sha256": current["sha256"]}})
    for item in authoritative:
        if item["path"] in {HISTORICAL_REGISTRY["path"], HISTORICAL_HEALTH["path"]}:
            continue
        input_path = current_paths.get(item["path"], ROOT / item["path"])
        identity = artifact_identity(input_path, item["path"])
        current_inputs.append(identity)
        if identity["sha256"] != item["sha256"]:
            mismatches.append({"code": "non_source_authoritative_input_changed", "path": item["path"], "expected": item["sha256"], "actual": identity["sha256"]})

    current_health_value = load_json(health_path)
    health_facts = []
    for cause in mapping["cause_mappings"]:
        for basis in cause.get("source_basis", []):
            if basis.get("type") != "registry_fact" or basis.get("artifact") != HISTORICAL_HEALTH["path"]:
                continue
            try:
                actual = pointer_get(current_health_value, basis["json_pointer"])
                status = "matched" if actual == basis["equals"] else "mismatch"
            except (KeyError, IndexError, TypeError, ValueError):
                actual = None
                status = "missing"
            health_facts.append({"cause": cause["cause"], "path": basis["artifact"], "json_pointer": basis["json_pointer"], "expected": basis["equals"], "actual": actual, "status": status})
            if status != "matched":
                mismatches.append({"code": f"health_pointer_{status}", "path": f"{basis['artifact']}#{basis['json_pointer']}", "expected": basis["equals"], "actual": actual})

    try:
        registry_value = load_json(registry_path)
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        registry_value = None
    registry_facts, registry_mismatches = evaluate_registry_identities(registry_value, proof)
    mismatches.extend(registry_mismatches)

    return {
        "schema_version": "datapan.diagnostic-current-source-applicability.v1",
        "status": "historical_scope_unchanged" if not mismatches else "revalidation_required",
        "historical_inputs": historical_inputs,
        "historical_registry_identity": dict(HISTORICAL_REGISTRY),
        "archived_health_catalog": verify_archive_provenance(),
        "current_inputs": {
            "registry": current_registry,
            "health_catalog": current_health,
            "other_authoritative_inputs": current_inputs,
        },
        "checked_facts": {"health_pointers": health_facts, "registry_datasets": registry_facts},
        "mismatches": mismatches,
        "authority": {
            "current_source_compatibility_approved": False,
            "current_runtime_evidence_accepted": False,
            "current_consumer_approval": False,
            "release_readiness_granted": False,
            "publication_allowed": False,
            "human_acceptance_granted": False,
        },
    }


def render_report(value: dict[str, Any]) -> bytes:
    return (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode("utf-8")


def validate_schema(value: Any) -> None:
    schema = load_json(SCHEMA)
    jsonschema.Draft202012Validator(schema, format_checker=jsonschema.FormatChecker()).validate(value)


def validate_report(value: Any) -> None:
    validate_schema(value)
    expected = build_report()
    if value != expected:
        raise ValueError("current-source applicability receipt differs from independently recomputed inputs")


def validate_current_publication_gate(value: dict[str, Any]) -> None:
    authority = value.get("authority", {})
    if value.get("status") != "historical_scope_unchanged":
        raise ValueError("current diagnostic publication blocked: current source requires revalidation")
    if authority.get("current_source_compatibility_approved") is not True or authority.get("publication_allowed") is not True:
        raise ValueError("current diagnostic publication blocked: no explicit current diagnostic publication authority")
