#!/usr/bin/env python3
"""Materialize the manifest-bound registry from a versioned public mirror."""

from __future__ import annotations

import argparse
import hashlib
import json
import pathlib
import shutil
import sys
import tempfile
import urllib.error
import urllib.request
from typing import Any

AVAILABILITY_EXIT = 20
INTEGRITY_EXIT = 21


class AvailabilityError(RuntimeError):
    pass


class IntegrityError(RuntimeError):
    pass


def load_object(path: pathlib.Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise IntegrityError(f"cannot read {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise IntegrityError(f"{path} must contain a JSON object")
    return value


def file_sha256(path: pathlib.Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def registry_identity(manifest: dict[str, Any]) -> tuple[pathlib.Path, int, str]:
    source, artifacts = manifest.get("source_registry"), manifest.get("artifacts")
    if not isinstance(source, str) or not isinstance(artifacts, list):
        raise IntegrityError("manifest is missing source_registry or artifacts")
    matches = [item for item in artifacts if isinstance(item, dict) and item.get("path") == source]
    if len(matches) != 1:
        raise IntegrityError(f"manifest must bind exactly one source registry: {source}")
    size, digest = matches[0].get("bytes"), matches[0].get("sha256")
    if not isinstance(size, int) or size <= 0 or not isinstance(digest, str) or len(digest) != 64:
        raise IntegrityError("manifest registry artifact has invalid bytes or sha256")
    return pathlib.Path(source), size, digest


def validate(path: pathlib.Path, expected_bytes: int, expected_sha256: str) -> None:
    if not path.exists():
        raise IntegrityError(f"registry is missing: {path}")
    actual_bytes = path.stat().st_size
    if actual_bytes != expected_bytes:
        raise IntegrityError(f"registry bytes expected {expected_bytes}, got {actual_bytes}")
    actual_sha256 = file_sha256(path)
    if actual_sha256 != expected_sha256:
        raise IntegrityError(f"registry sha256 expected {expected_sha256}, got {actual_sha256}")


def mirror_url(policy: dict[str, Any], registry_path: pathlib.Path, digest: str) -> str:
    mirror = policy.get("canonical_registry")
    if not isinstance(mirror, dict):
        raise IntegrityError("policy.canonical_registry must be an object")
    if mirror.get("manifest_sha256") != digest:
        raise IntegrityError("distribution policy is stale for the current manifest registry sha256")
    repository, revision, remote_path = mirror.get("repository"), mirror.get("revision"), mirror.get("path")
    if not all(isinstance(value, str) and value for value in (repository, revision, remote_path)):
        raise IntegrityError("distribution policy requires repository, revision, and path")
    if len(revision) != 40 or any(char not in "0123456789abcdef" for char in revision):
        raise IntegrityError("distribution revision must be a full immutable commit SHA")
    if remote_path != registry_path.as_posix():
        raise IntegrityError("distribution path must match manifest source_registry")
    return f"https://huggingface.co/datasets/{repository}/resolve/{revision}/{remote_path}?download=true"


def download(url: str, destination: pathlib.Path) -> None:
    request = urllib.request.Request(url, headers={"User-Agent": "datapan-registry-materializer/1"})
    try:
        with urllib.request.urlopen(request, timeout=120) as response, destination.open("wb") as output:
            shutil.copyfileobj(response, output, length=1024 * 1024)
    except (OSError, urllib.error.URLError) as exc:
        raise AvailabilityError(f"public registry mirror unavailable: {exc}") from exc


def materialize(policy_path: pathlib.Path, manifest_path: pathlib.Path, output: pathlib.Path | None) -> dict[str, Any]:
    manifest, policy = load_object(manifest_path), load_object(policy_path)
    registry_path, expected_bytes, expected_sha256 = registry_identity(manifest)
    destination = output or registry_path
    if destination.exists():
        try:
            validate(destination, expected_bytes, expected_sha256)
            return {"status": "reused", "source": "working_tree", "path": str(destination), "sha256": expected_sha256}
        except IntegrityError:
            pass
    url = mirror_url(policy, registry_path, expected_sha256)
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=destination.parent, prefix=f".{destination.name}.", delete=False) as handle:
        temporary = pathlib.Path(handle.name)
    try:
        download(url, temporary)
        validate(temporary, expected_bytes, expected_sha256)
        temporary.replace(destination)
    finally:
        temporary.unlink(missing_ok=True)
    return {"status": "materialized", "source": "huggingface", "path": str(destination), "sha256": expected_sha256}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--policy", type=pathlib.Path, default=pathlib.Path("policy/registry-distribution.json"))
    parser.add_argument("--manifest", type=pathlib.Path, default=pathlib.Path("manifest.json"))
    parser.add_argument("--output", type=pathlib.Path)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    try:
        if args.check:
            manifest, policy = load_object(args.manifest), load_object(args.policy)
            registry_path, size, digest = registry_identity(manifest)
            mirror_url(policy, registry_path, digest)
            validate(args.output or registry_path, size, digest)
            result = {"status": "verified", "path": str(args.output or registry_path), "sha256": digest}
        else:
            result = materialize(args.policy, args.manifest, args.output)
    except AvailabilityError as exc:
        print(json.dumps({"status": "availability_error", "error": str(exc)}), file=sys.stderr)
        return AVAILABILITY_EXIT
    except IntegrityError as exc:
        print(json.dumps({"status": "integrity_error", "error": str(exc)}), file=sys.stderr)
        return INTEGRITY_EXIT
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
