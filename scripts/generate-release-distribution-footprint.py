#!/usr/bin/env python3
"""Generate or check release distribution footprint evidence."""

from __future__ import annotations

import argparse
import hashlib
import json
import pathlib
import sys
from typing import Any

try:
    import jsonschema
except ImportError as exc:  # pragma: no cover - environment guard
    raise SystemExit("missing dependency: install jsonschema before generating distribution footprint evidence") from exc


CANONICAL_REGISTRY_PATH = "data/data-go-kr.registry.json"
DEFAULT_MANIFEST = pathlib.Path("manifest.json")
DEFAULT_SCHEMA = pathlib.Path("schemas/datapan.release-distribution-footprint.v1.schema.json")
DEFAULT_OUTPUT = pathlib.Path("reports/release-distribution-footprint.json")
SCHEMA_VERSION = "datapan.release-distribution-footprint.v1"
LARGE_MONOLITH_THRESHOLD_BYTES = 100_000_000


def load_json(path: pathlib.Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        value = json.load(handle)
    if not isinstance(value, dict):
        raise ValueError(f"{path} must contain a JSON object")
    return value


def render_json(value: dict[str, Any]) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2) + "\n"


def file_digest(path: pathlib.Path) -> tuple[int, str]:
    data = path.read_bytes()
    return len(data), hashlib.sha256(data).hexdigest()


def as_list(value: object, label: str) -> list[Any]:
    if not isinstance(value, list):
        raise ValueError(f"{label} must be an array")
    return value


def manifest_artifacts(manifest: dict[str, Any]) -> list[dict[str, Any]]:
    artifacts: list[dict[str, Any]] = []
    for index, raw_artifact in enumerate(as_list(manifest.get("artifacts"), "manifest.artifacts")):
        if not isinstance(raw_artifact, dict):
            raise ValueError(f"manifest.artifacts[{index}] must be an object")
        path = raw_artifact.get("path")
        kind = raw_artifact.get("kind")
        bytes_value = raw_artifact.get("bytes")
        sha256 = raw_artifact.get("sha256")
        if not isinstance(path, str) or not path:
            raise ValueError(f"manifest.artifacts[{index}].path must be a non-empty string")
        if not isinstance(kind, str) or not kind:
            raise ValueError(f"manifest.artifacts[{index}].kind must be a non-empty string")
        if not isinstance(bytes_value, int) or bytes_value < 0:
            raise ValueError(f"manifest.artifacts[{index}].bytes must be a non-negative integer")
        if not isinstance(sha256, str) or len(sha256) != 64:
            raise ValueError(f"manifest.artifacts[{index}].sha256 must be a 64-character string")
        artifacts.append(raw_artifact)
    return artifacts


def validate_checked_in_artifacts(artifacts: list[dict[str, Any]], output_path: pathlib.Path) -> None:
    output_name = output_path.as_posix()
    for artifact in artifacts:
        path_value = str(artifact["path"])
        if path_value == output_name:
            continue
        path = pathlib.Path(path_value)
        if not path.is_file():
            raise ValueError(f"manifest artifact is missing: {path_value}")
        bytes_value, sha256 = file_digest(path)
        if artifact.get("bytes") != bytes_value:
            raise ValueError(f"manifest artifact {path_value} has stale bytes")
        if artifact.get("sha256") != sha256:
            raise ValueError(f"manifest artifact {path_value} has stale sha256")


def build_report(manifest: dict[str, Any], *, manifest_path: pathlib.Path, output_path: pathlib.Path) -> dict[str, Any]:
    generated_at = manifest.get("generated_at")
    if not isinstance(generated_at, str) or not generated_at:
        raise ValueError("manifest.generated_at must be a non-empty string")
    if manifest_path.as_posix() != DEFAULT_MANIFEST.as_posix():
        raise ValueError("release distribution footprint currently expects manifest.json as the manifest path")

    artifacts = manifest_artifacts(manifest)
    validate_checked_in_artifacts(artifacts, output_path)
    artifacts_by_path = {str(artifact["path"]): artifact for artifact in artifacts}
    registry = artifacts_by_path.get(CANONICAL_REGISTRY_PATH)
    if registry is None or registry.get("kind") != "registry":
        raise ValueError(f"manifest must include {CANONICAL_REGISTRY_PATH} with kind=registry")

    artifact_count = manifest.get("artifact_count")
    if artifact_count != len(artifacts):
        raise ValueError("manifest.artifact_count must match manifest.artifacts length")

    output_name = output_path.as_posix()
    manifest_bound_bytes = sum(int(artifact["bytes"]) for artifact in artifacts if artifact["path"] != output_name)
    registry_bytes = int(registry["bytes"])
    status = (
        "large_monolith_shard_additive"
        if registry_bytes > LARGE_MONOLITH_THRESHOLD_BYTES
        else "within_monolith_budget"
    )
    largest = sorted(
        (
            {
                "path": str(artifact["path"]),
                "kind": str(artifact["kind"]),
                "bytes": int(artifact["bytes"]),
                "sha256": str(artifact["sha256"]),
            }
            for artifact in artifacts
            if artifact["path"] != output_name
        ),
        key=lambda artifact: (-artifact["bytes"], artifact["path"]),
    )[:5]

    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at": generated_at,
        "goal_issue": 344,
        "footprint_ticket": 393,
        "provider": "datapan-registry",
        "inputs": {
            "manifest": DEFAULT_MANIFEST.as_posix(),
            "self_artifact_excluded": True,
        },
        "summary": {
            "artifact_count": artifact_count,
            "schema_artifacts": sum(1 for artifact in artifacts if artifact["kind"] == "schema"),
            "manifest_bound_bytes_excluding_self": manifest_bound_bytes,
            "canonical_registry_path": CANONICAL_REGISTRY_PATH,
            "canonical_registry_bytes": registry_bytes,
            "large_monolith_threshold_bytes": LARGE_MONOLITH_THRESHOLD_BYTES,
            "registry_footprint_status": status,
            "canonical_registry_required": True,
            "shard_distribution_required": False,
            "monolith_fallback_required": True,
        },
        "distribution_boundary": {
            "canonical_registry_compatible": True,
            "release_package_includes_monolith": True,
            "shard_archive_status": "optional_additive_asset",
            "consumer_effect": "canonical_registry_required_shards_optional",
            "next_distribution_action": "prove_shard_preferred_install_with_canonical_fallback_before_requiring_shards",
        },
        "largest_artifacts": largest,
    }


def validate_schema(report: dict[str, Any], schema_path: pathlib.Path) -> None:
    schema = load_json(schema_path)
    validator = jsonschema.Draft202012Validator(schema)
    errors = sorted(validator.iter_errors(report), key=lambda error: list(error.path))
    if errors:
        rendered = []
        for error in errors:
            location = ".".join(str(part) for part in error.path) or "<root>"
            rendered.append(f"{location}: {error.message}")
        raise ValueError("; ".join(rendered))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", default=DEFAULT_MANIFEST, type=pathlib.Path)
    parser.add_argument("--schema", default=DEFAULT_SCHEMA, type=pathlib.Path)
    parser.add_argument("--output", default=DEFAULT_OUTPUT, type=pathlib.Path)
    parser.add_argument("--check", action="store_true", help="fail when checked-in footprint evidence is stale")
    args = parser.parse_args()

    try:
        report = build_report(load_json(args.manifest), manifest_path=args.manifest, output_path=args.output)
        validate_schema(report, args.schema)
    except Exception as exc:  # noqa: BLE001 - release operators need the failed invariant
        print(f"FAIL generate release distribution footprint: {exc}", file=sys.stderr)
        return 1

    rendered = render_json(report)
    if args.check:
        if not args.output.exists():
            print(f"FAIL {args.output}: missing release distribution footprint", file=sys.stderr)
            return 1
        current = args.output.read_text(encoding="utf-8")
        if current != rendered:
            print(
                f"FAIL {args.output}: stale release distribution footprint; "
                "run `python3 scripts/generate-release-distribution-footprint.py`",
                file=sys.stderr,
            )
            return 1
        print(
            "ok "
            f"{args.output} "
            f"(canonical_registry_bytes={report['summary']['canonical_registry_bytes']})"
        )
        return 0

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(rendered, encoding="utf-8")
    print(
        "wrote "
        f"{args.output} "
        f"(canonical_registry_bytes={report['summary']['canonical_registry_bytes']})"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
