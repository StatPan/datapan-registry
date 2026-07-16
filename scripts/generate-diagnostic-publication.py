#!/usr/bin/env python3
"""Promote the accepted diagnostic candidate into deterministic public artifacts."""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import pathlib
import re
import sys
from typing import Any
from urllib.parse import parse_qsl, urlsplit


ROOT = pathlib.Path(__file__).resolve().parents[1]
CANDIDATE = pathlib.Path("drafts/diagnostic-envelope/release-candidate/diagnostic-release-candidate.v1.json")
CANDIDATE_SOURCE_HEAD = "5343ebc4477640409d76cac5bee71e0824e48d59"
CANDIDATE_MERGE_COMMIT = "114c4e4a043bc495ada04e5c85fe8bed4eaf1fc3"
CANDIDATE_BINDING = "ac847cb158eb432e72e78d194a94542db5860062b9b869c42f6d736e4f649016"
GENERATED_AT = "2026-07-16T10:30:00Z"

SCHEMA_SOURCE = pathlib.Path("drafts/diagnostic-envelope/datapan.diagnostic-envelope.v1.schema.json")
SCHEMA_PUBLIC = pathlib.Path("schemas/datapan.diagnostic-envelope.v1.schema.json")
CONTRACT_SOURCE = pathlib.Path("drafts/diagnostic-envelope/consumer-contract.v1.json")
CONTRACT_PUBLIC = pathlib.Path("policy/diagnostic-envelope-consumer-contract.v1.json")
MAPPING_SOURCE = pathlib.Path("drafts/diagnostic-envelope/data-go-kr-evidence-mapping.v1.json")
MAPPING_PUBLIC = pathlib.Path("policy/data-go-kr-diagnostic-evidence-mapping.v1.json")
VOCABULARY_PUBLIC = pathlib.Path("policy/diagnostic-cause-action-vocabulary.v1.json")
READINESS_PUBLIC = pathlib.Path("reports/diagnostic-publication-readiness.json")
READINESS_SCHEMA = pathlib.Path("schemas/datapan.diagnostic-publication-readiness.v1.schema.json")

COMPATIBILITY_SOURCES = {
    name: pathlib.Path(f"drafts/diagnostic-envelope/consumer-compatibility/{name}.v1.json")
    for name in ("datapan-cli", "datapan-health", "datapan-web")
}
COMPATIBILITY_PUBLIC = {
    name: pathlib.Path(f"reports/diagnostic-consumer-compatibility/{name}.v1.json")
    for name in COMPATIBILITY_SOURCES
}
FIXTURE_SOURCES = sorted(pathlib.Path("drafts/diagnostic-envelope/fixtures").glob("*.json"))
FIXTURE_PUBLIC = {
    path: pathlib.Path("examples/diagnostic-envelope") / path.name for path in FIXTURE_SOURCES
}

PATH_REPLACEMENTS = {
    SCHEMA_SOURCE.as_posix(): SCHEMA_PUBLIC.as_posix(),
    CONTRACT_SOURCE.as_posix(): CONTRACT_PUBLIC.as_posix(),
    MAPPING_SOURCE.as_posix(): MAPPING_PUBLIC.as_posix(),
    "drafts/diagnostic-envelope/fixtures/": "examples/diagnostic-envelope/",
}

FORBIDDEN_KEYS = {
    "authorization",
    "auth_header",
    "api_key",
    "service_key",
    "secret",
    "secret_hash",
    "credential_hash",
    "token_hash",
    "access_token",
    "refresh_token",
    "raw_response",
    "raw_response_body",
    "raw_provider_body",
    "raw_provider_response",
    "response_body",
    "response_rows",
    "authorization_header",
    "request_headers",
    "live_status_history",
    "live_history",
    "user_telemetry",
}
SENSITIVE_QUERY_KEYS = {
    "key",
    "api_key",
    "apikey",
    "servicekey",
    "service_key",
    "token",
    "access_token",
    "authorization",
}
SECRET_ASSIGNMENT = re.compile(
    r"(?i)(?:api[_-]?key|service[_-]?key|access[_-]?token|authorization)\s*[:=]\s*[^\s{}\[\],]+"
)


def stable_bytes(value: object) -> bytes:
    return (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode("utf-8")


def load(path: pathlib.Path) -> dict[str, Any]:
    value = json.loads((ROOT / path).read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"{path} must contain a JSON object")
    return value


def digest(data: bytes) -> dict[str, Any]:
    return {"bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()}


def replace_paths(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: replace_paths(item) for key, item in value.items()}
    if isinstance(value, list):
        return [replace_paths(item) for item in value]
    if isinstance(value, str):
        for source, destination in PATH_REPLACEMENTS.items():
            value = value.replace(source, destination)
    return value


def promote(value: dict[str, Any]) -> dict[str, Any]:
    promoted = replace_paths(copy.deepcopy(value))
    if promoted.get("status") == "draft":
        promoted["status"] = "stable"
    return promoted


def artifact_record(path: pathlib.Path, data: bytes, kind: str) -> dict[str, Any]:
    return {"path": path.as_posix(), "kind": kind, **digest(data)}


def source_record(path: pathlib.Path) -> dict[str, Any]:
    return {"path": path.as_posix(), **digest((ROOT / path).read_bytes())}


def validate_candidate(candidate: dict[str, Any]) -> None:
    if candidate.get("status") != "ready_for_publication_review":
        raise ValueError("diagnostic candidate is not ready for publication review")
    if candidate.get("binding_sha256") != CANDIDATE_BINDING:
        raise ValueError("diagnostic candidate binding does not match the accepted candidate")
    authority = candidate.get("authority")
    if not isinstance(authority, dict) or any(value is not False for value in authority.values()):
        raise ValueError("accepted candidate must not grant release or runtime authority")
    contracts = candidate.get("binding", {}).get("contracts", [])
    candidate_sources = {
        item.get("path"): item.get("sha256") for item in contracts if isinstance(item, dict)
    }
    required_sources = [SCHEMA_SOURCE, CONTRACT_SOURCE, MAPPING_SOURCE, *COMPATIBILITY_SOURCES.values()]
    for path in required_sources:
        actual = hashlib.sha256((ROOT / path).read_bytes()).hexdigest()
        if candidate_sources.get(path.as_posix()) != actual:
            raise ValueError(f"accepted candidate source identity mismatch: {path}")


def inspect_safe(value: Any, path: str = "$") -> None:
    if isinstance(value, dict):
        for key, item in value.items():
            normalized = key.lower().replace("-", "_")
            if normalized in FORBIDDEN_KEYS:
                raise ValueError(f"forbidden release field at {path}.{key}")
            inspect_safe(item, f"{path}.{key}")
        return
    if isinstance(value, list):
        for index, item in enumerate(value):
            inspect_safe(item, f"{path}[{index}]")
        return
    if not isinstance(value, str):
        return
    if SECRET_ASSIGNMENT.search(value):
        raise ValueError(f"credential-like assignment in release value at {path}")
    if value.startswith(("http://", "https://")):
        query_keys = {key.lower() for key, _ in parse_qsl(urlsplit(value).query, keep_blank_values=True)}
        forbidden = query_keys.intersection(SENSITIVE_QUERY_KEYS)
        if forbidden:
            raise ValueError(f"credential-bearing URL at {path}: {sorted(forbidden)}")


def build() -> dict[pathlib.Path, bytes]:
    candidate = load(CANDIDATE)
    validate_candidate(candidate)

    outputs: dict[pathlib.Path, bytes] = {}
    schema = promote(load(SCHEMA_SOURCE))
    schema["title"] = "Datapan Diagnostic Envelope v1"
    schema["$comment"] = (
        "Stable public contract. Registry owns the bounded vocabulary and evidence shape; "
        "consumers own live inference, presentation, and mutable history."
    )
    outputs[SCHEMA_PUBLIC] = stable_bytes(schema)

    contract = promote(load(CONTRACT_SOURCE))
    contract["compatibility"]["draft_instances_are_runtime_authority"] = False
    outputs[CONTRACT_PUBLIC] = stable_bytes(contract)

    mapping = promote(load(MAPPING_SOURCE))
    outputs[MAPPING_PUBLIC] = stable_bytes(mapping)

    vocabulary = {
        "schema_version": "datapan.diagnostic-cause-action-vocabulary.v1",
        "status": "stable",
        "generated_at": GENERATED_AT,
        "source_contract": {
            "path": CONTRACT_PUBLIC.as_posix(),
            **digest(outputs[CONTRACT_PUBLIC]),
        },
        "causes": contract["cause_catalog"],
        "compatibility": {
            "unknown_code_behavior": "map_to_unknown_and_gather_more_evidence",
            "same_version_enum_additions_allowed": False,
            "deprecation": "publish_a_new_version_before_removing_or_redefining_any_id",
        },
    }
    outputs[VOCABULARY_PUBLIC] = stable_bytes(vocabulary)

    schema_identity = digest(outputs[SCHEMA_PUBLIC])
    mapping_identity = digest(outputs[MAPPING_PUBLIC])
    for consumer, source in COMPATIBILITY_SOURCES.items():
        compatibility = promote(load(source))
        compatibility["schema_contract"] = {
            "path": SCHEMA_PUBLIC.as_posix(),
            "sha256": schema_identity["sha256"],
        }
        compatibility["mapping_contract"] = {
            "path": MAPPING_PUBLIC.as_posix(),
            "sha256": mapping_identity["sha256"],
        }
        compatibility["publication_proof"] = {
            "candidate_binding_sha256": CANDIDATE_BINDING,
            "candidate_merge_commit": CANDIDATE_MERGE_COMMIT,
            "scope": "prepublication_consumer_compatibility",
            "runtime_authority": False,
        }
        outputs[COMPATIBILITY_PUBLIC[consumer]] = stable_bytes(compatibility)

    for source, destination in FIXTURE_PUBLIC.items():
        outputs[destination] = stable_bytes(load(source))

    public_records = []
    for path in sorted(outputs, key=lambda item: item.as_posix()):
        if path == SCHEMA_PUBLIC:
            kind = "schema"
        elif path == VOCABULARY_PUBLIC:
            kind = "diagnostic_vocabulary"
        elif path == CONTRACT_PUBLIC:
            kind = "diagnostic_consumer_contract"
        elif path == MAPPING_PUBLIC:
            kind = "diagnostic_evidence_mapping"
        elif path in COMPATIBILITY_PUBLIC.values():
            kind = "diagnostic_consumer_compatibility"
        else:
            kind = "diagnostic_example"
        public_records.append(artifact_record(path, outputs[path], kind))
    public_records.append(
        artifact_record(
            READINESS_SCHEMA,
            (ROOT / READINESS_SCHEMA).read_bytes(),
            "schema",
        )
    )
    public_records.sort(key=lambda item: item["path"])

    readiness = {
        "schema_version": "datapan.diagnostic-publication-readiness.v1",
        "generated_at": GENERATED_AT,
        "status": "prepared_for_exact_head_review",
        "candidate": {
            "source_head": CANDIDATE_SOURCE_HEAD,
            "merge_commit": CANDIDATE_MERGE_COMMIT,
            "binding_sha256": CANDIDATE_BINDING,
            "source": source_record(CANDIDATE),
        },
        "artifacts": public_records,
        "exclusion_policy": {
            "status": "passed",
            "forbidden": [
                "credentials_or_secret_hashes",
                "authorization_headers",
                "credential_bearing_urls",
                "raw_provider_response_bodies",
                "live_status_history",
                "user_telemetry",
            ],
        },
        "gates": {
            "exact_head_independent_review": "pending",
            "hosted_ci": "pending",
            "tag_or_github_release": "not_authorized",
            "immutable_distribution_publication": "not_authorized",
            "anonymous_immutable_fetch": "blocked_until_publication",
            "datapan_web_adoption": "blocked_until_anonymous_immutable_fetch",
        },
        "authority": {
            "publishing_allowed": False,
            "runtime_authority": False,
            "live_history_authority": False,
            "consumer_deployment_allowed": False,
        },
        "anonymous_fetch_handoff": {
            "tool": "scripts/huggingface_registry_distribution.py verify-remote",
            "requirements": [
                "anonymous_http",
                "full_nonzero_immutable_payload_revision",
                "release_manifest_identity_match",
                "all_diagnostic_artifact_bytes_and_sha256_match",
            ],
        },
        "web_adoption_handoff": {
            "repository": "StatPan/datapan",
            "consumer": "datapan-web",
            "state": "post_publication_blocked",
            "unblock_on": "anonymous_immutable_fetch_verified_for_every_artifact",
            "ownership": "Web owns presentation and mutable history; Registry owns facts, vocabulary, and immutable artifact identity.",
        },
    }
    inspect_safe({path.as_posix(): json.loads(data) for path, data in outputs.items()})
    inspect_safe(readiness)
    outputs[READINESS_PUBLIC] = stable_bytes(readiness)
    return outputs


def write(outputs: dict[pathlib.Path, bytes]) -> None:
    for path, data in outputs.items():
        target = ROOT / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)


def check(outputs: dict[pathlib.Path, bytes]) -> list[str]:
    drift = []
    for path, expected in outputs.items():
        target = ROOT / path
        if not target.is_file() or target.read_bytes() != expected:
            drift.append(path.as_posix())
    return drift


def validate_release_bindings(outputs: dict[pathlib.Path, bytes]) -> None:
    manifest = load(pathlib.Path("manifest.json"))
    artifacts = manifest.get("artifacts")
    if not isinstance(artifacts, list) or manifest.get("artifact_count") != len(artifacts):
        raise ValueError("release manifest artifact count is inconsistent")
    by_path = {
        item.get("path"): item for item in artifacts if isinstance(item, dict) and isinstance(item.get("path"), str)
    }
    if len(by_path) != len(artifacts):
        raise ValueError("release manifest contains duplicate or malformed artifact paths")
    for path, data in outputs.items():
        record = by_path.get(path.as_posix())
        if record is None:
            raise ValueError(f"release manifest is missing diagnostic artifact: {path}")
        identity = digest(data)
        if record.get("bytes") != identity["bytes"] or record.get("sha256") != identity["sha256"]:
            raise ValueError(f"release manifest identity mismatch: {path}")
    if any(str(path).startswith("drafts/diagnostic-envelope/") for path in by_path):
        raise ValueError("release manifest must not publish diagnostic draft paths")

    index = load(pathlib.Path("schemas/index.json"))
    schemas = index.get("schemas")
    if not isinstance(schemas, list) or index.get("count") != len(schemas):
        raise ValueError("schema index count is inconsistent")
    for schema_path, schema_data in (
        (SCHEMA_PUBLIC, outputs[SCHEMA_PUBLIC]),
        (READINESS_SCHEMA, (ROOT / READINESS_SCHEMA).read_bytes()),
    ):
        schema_record = next(
            (item for item in schemas if isinstance(item, dict) and item.get("path") == schema_path.as_posix()),
            None,
        )
        schema_identity = digest(schema_data)
        if schema_record is None or any(
            schema_record.get(key) != value for key, value in schema_identity.items()
        ):
            raise ValueError(f"public diagnostic schema is not bound by the schema index: {schema_path}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--write", action="store_true")
    mode.add_argument("--check", action="store_true")
    args = parser.parse_args()
    try:
        outputs = build()
        if args.check:
            drift = check(outputs)
            if drift:
                raise ValueError(f"diagnostic publication drift: {', '.join(drift)}")
            validate_release_bindings(outputs)
        else:
            write(outputs)
    except Exception as exc:  # noqa: BLE001 - surface the failed publication invariant
        print(f"FAIL diagnostic publication: {exc}", file=sys.stderr)
        return 1
    action = "checked" if args.check else "wrote"
    print(f"{action} diagnostic publication artifacts (artifacts={len(outputs)}, binding={CANDIDATE_BINDING})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
