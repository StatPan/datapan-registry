#!/usr/bin/env python3
"""Generate deterministic Registry release-version change evidence.

The version allocation itself stays with the release operator.  This tool only
compares a candidate's immutable inputs with the last allocated baseline and
fails closed when the requested manifest version does not match that change.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import pathlib
import re
import sys
from typing import Any

try:
    import jsonschema
except ImportError as exc:  # pragma: no cover - environment guard
    raise SystemExit("missing dependency: install jsonschema before generating version evidence") from exc


DEFAULT_MANIFEST = pathlib.Path("manifest.json")
DEFAULT_POLICY = pathlib.Path("policy/release-versioning.json")
DEFAULT_SCHEMA = pathlib.Path("schemas/datapan.release-version-decision.v1.schema.json")
DEFAULT_OUTPUT = pathlib.Path("reports/release-version-decision.json")
OUTPUT_PATH = DEFAULT_OUTPUT.as_posix()
REGISTRY_PATH = "data/data-go-kr.registry.json"
REQUEST_ONLY_PROFILE_PATH = "reports/data-go-kr/request-only-client-profile.json"
SCHEMA_VERSION = "datapan.release-version-decision.v1"
SEMVER = re.compile(r"^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)(?:-[0-9A-Za-z.-]+)?$")


def load_json(path: pathlib.Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"{path} must contain a JSON object")
    return value


def render_json(value: dict[str, Any]) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2) + "\n"


def digest(value: object) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def release_version(value: object, label: str) -> tuple[int, int, int]:
    if not isinstance(value, str) or not (match := SEMVER.fullmatch(value)):
        raise ValueError(f"{label} must be a semantic version")
    return tuple(int(part) for part in match.groups())


def artifacts(manifest: dict[str, Any]) -> list[dict[str, Any]]:
    value = manifest.get("artifacts")
    if not isinstance(value, list):
        raise ValueError("manifest.artifacts must be an array")
    result: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, artifact in enumerate(value):
        if not isinstance(artifact, dict):
            raise ValueError(f"manifest.artifacts[{index}] must be an object")
        path = artifact.get("path")
        if not isinstance(path, str) or not path:
            raise ValueError(f"manifest.artifacts[{index}].path must be non-empty")
        if path in seen:
            raise ValueError(f"manifest.artifacts has duplicate path: {path}")
        seen.add(path)
        result.append(artifact)
    if manifest.get("artifact_count") != len(result):
        raise ValueError("manifest.artifact_count must match manifest.artifacts")
    return result


def version_artifact(artifact: dict[str, Any]) -> dict[str, Any]:
    """Normalize an artifact for version input without a derived-digest cycle.

    The request-only profile's existence, kind, and schema are versioned. Its
    bytes and digest are derived from this manifest and from this decision, so
    including those two fields would make the two generated release artifacts
    continually invalidate one another.
    """
    if artifact.get("path") != REQUEST_ONLY_PROFILE_PATH:
        return artifact
    return {key: value for key, value in artifact.items() if key not in {"bytes", "sha256"}}


def version_input(manifest: dict[str, Any]) -> dict[str, Any]:
    all_artifacts = artifacts(manifest)
    included = [version_artifact(artifact) for artifact in all_artifacts if artifact.get("path") != OUTPUT_PATH]
    registry = next((artifact for artifact in included if artifact.get("path") == REGISTRY_PATH), None)
    if registry is None or not isinstance(registry.get("sha256"), str):
        raise ValueError(f"manifest must contain {REGISTRY_PATH} with sha256")

    inventory = [
        {
            "path": artifact.get("path"),
            "kind": artifact.get("kind"),
            "schema": artifact.get("schema"),
            "bytes": artifact.get("bytes"),
            "sha256": artifact.get("sha256"),
        }
        for artifact in included
    ]
    normalized_manifest = dict(manifest)
    normalized_manifest.pop("generated_at", None)
    normalized_manifest.pop("datapan_version", None)
    normalized_manifest["artifacts"] = included
    normalized_manifest["artifact_count"] = len(included)
    return {
        "source_revision": registry["sha256"],
        "manifest_digest": digest(normalized_manifest),
        "ordered_artifact_inventory_digest": digest(inventory),
        "artifact_count_excluding_decision": len(included),
    }


def require_input(value: object, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be an object")
    required = ("source_revision", "manifest_digest", "ordered_artifact_inventory_digest", "artifact_count_excluding_decision")
    for key in required:
        if key not in value:
            raise ValueError(f"{label}.{key} is required")
    if not all(isinstance(value[key], str) and re.fullmatch(r"[a-f0-9]{64}", value[key]) for key in required[:3]):
        raise ValueError(f"{label} digests must be SHA-256")
    if not isinstance(value["artifact_count_excluding_decision"], int) or value["artifact_count_excluding_decision"] < 1:
        raise ValueError(f"{label}.artifact_count_excluding_decision must be positive")
    return {key: value[key] for key in required}


def build_report(manifest: dict[str, Any], policy: dict[str, Any]) -> dict[str, Any]:
    if policy.get("schema_version") != "datapan.release-version-policy.v1":
        raise ValueError("unsupported release version policy")
    baseline = policy.get("baseline")
    if not isinstance(baseline, dict):
        raise ValueError("policy.baseline must be an object")
    baseline_version = baseline.get("datapan_version")
    baseline_tuple = release_version(baseline_version, "policy.baseline.datapan_version")
    baseline_input = require_input(baseline.get("input"), "policy.baseline.input")
    candidate_version = manifest.get("datapan_version")
    candidate_tuple = release_version(candidate_version, "manifest.datapan_version")
    current = version_input(manifest)
    changed = current != baseline_input
    if changed:
        if candidate_tuple <= baseline_tuple:
            raise ValueError("changed release input requires a version bump")
        decision = "changed_input_version_bumped"
    else:
        if candidate_version != baseline_version:
            raise ValueError("unchanged release input must not bump the version")
        decision = "no_change_no_bump"

    return {
        "schema_version": SCHEMA_VERSION,
        "goal_issue": 604,
        "versioning_ticket": 598,
        "policy": DEFAULT_POLICY.as_posix(),
        "allocation_authority": policy.get("allocation_authority"),
        "self_artifact_excluded": True,
        "baseline": {"datapan_version": baseline_version, "input": baseline_input},
        "candidate": {"datapan_version": candidate_version, "input": current},
        "decision": decision,
        "change_required": changed,
        "publication": {"tagged": False, "huggingface_published": False, "external_mutation": False},
        "consumer_compatibility": {
            "manifest_bound": True,
            "package_required_paths": [
                "reports/data-go-kr/operation-manifest.json",
                "schemas/datapan.data-go-kr-operation-manifest.v1.schema.json",
                REQUEST_ONLY_PROFILE_PATH,
                "schemas/datapan.request-only-client-profile.v1.schema.json",
                OUTPUT_PATH,
                DEFAULT_SCHEMA.as_posix(),
            ],
            "proof": "package archive validation checks every manifest artifact path and checksum",
        },
    }


def validate_schema(report: dict[str, Any], schema_path: pathlib.Path) -> None:
    schema = load_json(schema_path)
    errors = sorted(jsonschema.Draft202012Validator(schema).iter_errors(report), key=lambda error: list(error.path))
    if errors:
        rendered = [f"{'.'.join(str(part) for part in error.path) or '<root>'}: {error.message}" for error in errors]
        raise ValueError("; ".join(rendered))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=pathlib.Path, default=DEFAULT_MANIFEST)
    parser.add_argument("--policy", type=pathlib.Path, default=DEFAULT_POLICY)
    parser.add_argument("--schema", type=pathlib.Path, default=DEFAULT_SCHEMA)
    parser.add_argument("--output", type=pathlib.Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--check", action="store_true", help="fail when checked-in decision evidence is stale")
    parser.add_argument("--print-input", action="store_true", help="print normalized immutable input without reading policy")
    args = parser.parse_args()
    try:
        manifest = load_json(args.manifest)
        if args.print_input:
            print(render_json(version_input(manifest)), end="")
            return 0
        report = build_report(manifest, load_json(args.policy))
        validate_schema(report, args.schema)
    except Exception as exc:  # noqa: BLE001 - release operators need the failed invariant
        print(f"FAIL release version decision: {exc}", file=sys.stderr)
        return 1
    rendered = render_json(report)
    if args.check:
        if not args.output.is_file() or args.output.read_text(encoding="utf-8") != rendered:
            print(f"FAIL {args.output}: stale release version decision", file=sys.stderr)
            return 1
        print(f"ok {args.output} ({report['decision']})")
        return 0
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(rendered, encoding="utf-8")
    print(f"wrote {args.output} ({report['decision']})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
