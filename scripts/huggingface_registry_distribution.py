#!/usr/bin/env python3
"""Stage, publish, and independently verify the Registry Dataset distribution."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import pathlib
import shutil
import sys
import tempfile
import urllib.error
import urllib.request
from datetime import datetime, timezone
from typing import Any

SCHEMA_VERSION = "datapan.huggingface-distribution.v1"
DEFAULT_DATASET = "StatPan/datapan-registry"
INDEX_NAME = ".datapan-hf-artifacts.json"
POINTER_PATH = "release/distribution-manifest.json"


class DistributionError(RuntimeError):
    pass


def load_object(path: pathlib.Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise DistributionError(f"cannot read {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise DistributionError(f"{path} must contain a JSON object")
    return value


def sha256(path: pathlib.Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def safe_relative(value: str) -> pathlib.PurePosixPath:
    path = pathlib.PurePosixPath(value)
    if path.is_absolute() or not value or ".." in path.parts or "\\" in value:
        raise DistributionError(f"unsafe distribution path: {value}")
    return path


def artifact(path: pathlib.Path, remote: str, kind: str) -> dict[str, Any]:
    if not path.is_file():
        raise DistributionError(f"artifact is missing: {path}")
    size = path.stat().st_size
    if size < 1:
        raise DistributionError(f"artifact is empty: {path}")
    return {"path": safe_relative(remote).as_posix(), "kind": kind, "bytes": size, "sha256": sha256(path)}


def verify_local(path: pathlib.Path, record: dict[str, Any]) -> None:
    if not path.is_file():
        raise DistributionError(f"artifact is missing: {path}")
    if path.stat().st_size != record.get("bytes"):
        raise DistributionError(f"artifact byte mismatch: {record.get('path')}")
    if sha256(path) != record.get("sha256"):
        raise DistributionError(f"artifact SHA-256 mismatch: {record.get('path')}")


def stage(manifest_path: pathlib.Path, output: pathlib.Path, extras: list[str]) -> dict[str, Any]:
    manifest = load_object(manifest_path)
    records = manifest.get("artifacts")
    if manifest.get("schema_version") != "datapan.release-manifest.v1" or not isinstance(records, list):
        raise DistributionError("unsupported canonical release manifest")
    root = manifest_path.parent.resolve()
    if output.exists():
        shutil.rmtree(output)
    output.mkdir(parents=True)
    staged: list[dict[str, Any]] = []
    seen: set[str] = set()
    for record in records:
        if not isinstance(record, dict):
            raise DistributionError("release manifest artifact must be an object")
        remote, kind = record.get("path"), record.get("kind")
        if not isinstance(remote, str) or not isinstance(kind, str):
            raise DistributionError("release manifest artifact is missing path or kind")
        safe_relative(remote)
        source = root / pathlib.Path(remote)
        verify_local(source, record)
        destination = output / pathlib.Path(remote)
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, destination)
        staged.append(artifact(destination, remote, kind))
        seen.add(remote)
    release_manifest_remote = "manifest.json"
    destination = output / release_manifest_remote
    shutil.copyfile(manifest_path, destination)
    release_manifest = artifact(destination, release_manifest_remote, "release_manifest")
    for item in extras:
        if "=" not in item:
            raise DistributionError("--extra must use REMOTE=LOCAL")
        remote, local = item.split("=", 1)
        safe_relative(remote)
        if remote in seen or remote == release_manifest_remote:
            raise DistributionError(f"duplicate distribution path: {remote}")
        source = pathlib.Path(local)
        if not source.is_file():
            raise DistributionError(f"artifact is missing: {source}")
        destination = output / pathlib.Path(remote)
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, destination)
        staged.append(artifact(destination, remote, "distribution_asset"))
        seen.add(remote)
    staged.sort(key=lambda item: item["path"])
    index = {"release_manifest": release_manifest, "artifact_count": len(staged), "artifacts": staged}
    (output / INDEX_NAME).write_text(json.dumps(index, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return index


def finalize(stage_dir: pathlib.Path, dataset: str, revision: str, output: pathlib.Path | None = None) -> dict[str, Any]:
    if len(revision) != 40 or any(char not in "0123456789abcdef" for char in revision):
        raise DistributionError("payload revision must be a full immutable commit SHA")
    if len(dataset.split("/")) != 2 or any(not part for part in dataset.split("/")):
        raise DistributionError("dataset ID must be OWNER/NAME")
    index = load_object(stage_dir / INDEX_NAME)
    records = index.get("artifacts")
    if not isinstance(records, list) or index.get("artifact_count") != len(records):
        raise DistributionError("staged artifact index count mismatch")
    for record in records:
        verify_local(stage_dir / pathlib.Path(record["path"]), record)
    release_manifest = index.get("release_manifest")
    if not isinstance(release_manifest, dict):
        raise DistributionError("staged release manifest identity is missing")
    verify_local(stage_dir / pathlib.Path(release_manifest["path"]), release_manifest)
    pointer = {
        "schema_version": SCHEMA_VERSION,
        "generated_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        "dataset": {"id": dataset, "revision": revision},
        "release_manifest": release_manifest,
        "artifact_count": len(records),
        "artifacts": records,
    }
    target = output or stage_dir / POINTER_PATH
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(pointer, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return pointer


def resolve_url(dataset: str, revision: str, path: str) -> str:
    return f"https://huggingface.co/datasets/{dataset}/resolve/{revision}/{path}"


def download(url: str, destination: pathlib.Path) -> None:
    request = urllib.request.Request(url, headers={"User-Agent": "datapan-registry-distribution/1"})
    try:
        with urllib.request.urlopen(request, timeout=120) as response, destination.open("wb") as output:
            shutil.copyfileobj(response, output, length=1024 * 1024)
    except (OSError, urllib.error.URLError) as exc:
        raise DistributionError(f"download failed for {url}: {exc}") from exc


def required_artifact(value: str) -> tuple[str, str]:
    if "=" not in value:
        raise DistributionError("--require-artifact must use PATH=SHA256")
    path, expected_sha256 = value.split("=", 1)
    path = safe_relative(path).as_posix()
    if len(expected_sha256) != 64 or any(char not in "0123456789abcdef" for char in expected_sha256):
        raise DistributionError("required artifact SHA-256 must be 64 lowercase hexadecimal characters")
    return path, expected_sha256


def validate_remote_pointer(
    pointer: dict[str, Any],
    expected_revision: str | None,
    required: list[str],
) -> tuple[str, str, list[dict[str, Any]]]:
    if pointer.get("schema_version") != SCHEMA_VERSION:
        raise DistributionError("unsupported Hugging Face distribution manifest")
    dataset = pointer.get("dataset")
    records = pointer.get("artifacts")
    if not isinstance(dataset, dict) or not isinstance(records, list):
        raise DistributionError("distribution manifest is missing dataset or artifacts")
    revision, dataset_id = dataset.get("revision"), dataset.get("id")
    if (
        not isinstance(revision, str)
        or len(revision) != 40
        or any(char not in "0123456789abcdef" for char in revision)
        or revision == "0" * 40
    ):
        raise DistributionError("distribution manifest is missing a nonzero immutable revision")
    if expected_revision is not None:
        if (
            len(expected_revision) != 40
            or any(char not in "0123456789abcdef" for char in expected_revision)
            or expected_revision == "0" * 40
        ):
            raise DistributionError("expected revision must be a full nonzero immutable commit SHA")
        if revision != expected_revision:
            raise DistributionError(f"payload revision mismatch: {revision} != {expected_revision}")
    if not isinstance(dataset_id, str) or len(dataset_id.split("/")) != 2:
        raise DistributionError("distribution manifest dataset ID is invalid")
    if pointer.get("artifact_count") != len(records):
        raise DistributionError("distribution manifest artifact count mismatch")
    release_manifest = pointer.get("release_manifest")
    all_records = [release_manifest, *records]
    by_path: dict[str, dict[str, Any]] = {}
    for record in all_records:
        if not isinstance(record, dict):
            raise DistributionError("distribution artifact must be an object")
        remote = safe_relative(record.get("path", "")).as_posix()
        if remote in by_path:
            raise DistributionError(f"duplicate distribution artifact: {remote}")
        by_path[remote] = record
    for requirement in required:
        path, expected_sha256 = required_artifact(requirement)
        record = by_path.get(path)
        if record is None:
            raise DistributionError(f"required distribution artifact is missing: {path}")
        if record.get("sha256") != expected_sha256:
            raise DistributionError(f"required distribution artifact identity mismatch: {path}")
    return dataset_id, revision, all_records


def verify_remote(
    pointer_url: str,
    expected_revision: str | None = None,
    required: list[str] | None = None,
) -> dict[str, Any]:
    with tempfile.TemporaryDirectory() as raw:
        directory = pathlib.Path(raw)
        pointer_path = directory / "pointer.json"
        download(pointer_url, pointer_path)
        pointer = load_object(pointer_path)
        dataset_id, revision, all_records = validate_remote_pointer(
            pointer, expected_revision, required or []
        )
        for index, record in enumerate(all_records):
            remote = safe_relative(record.get("path", "")).as_posix()
            target = directory / f"artifact-{index}"
            download(resolve_url(dataset_id, revision, remote), target)
            verify_local(target, record)
    return {
        "status": "verified",
        "dataset": dataset_id,
        "revision": revision,
        "artifacts": len(all_records) - 1,
        "release_manifest": "verified",
    }


def publish(stage_dir: pathlib.Path, dataset: str, token: str) -> dict[str, Any]:
    if not token:
        raise DistributionError("HF_TOKEN is required for publication")
    try:
        from huggingface_hub import HfApi
    except ImportError as exc:
        raise DistributionError("huggingface_hub is required for publication") from exc
    api = HfApi(token=token)
    api.create_repo(repo_id=dataset, repo_type="dataset", exist_ok=True)
    payload = api.upload_folder(
        repo_id=dataset,
        repo_type="dataset",
        folder_path=str(stage_dir),
        ignore_patterns=[INDEX_NAME, POINTER_PATH],
        commit_message="Publish verified Datapan Registry payload",
    )
    revision = str(payload.oid)
    pointer = finalize(stage_dir, dataset, revision)
    pointer_commit = api.upload_file(
        repo_id=dataset,
        repo_type="dataset",
        path_or_fileobj=str(stage_dir / POINTER_PATH),
        path_in_repo=POINTER_PATH,
        commit_message=f"Point Registry distribution to {revision}",
    )
    return {"status": "published", "dataset": dataset, "payload_revision": revision, "pointer_revision": str(pointer_commit.oid), "artifacts": pointer["artifact_count"]}


def main() -> int:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    stage_parser = sub.add_parser("stage")
    stage_parser.add_argument("--manifest", type=pathlib.Path, default=pathlib.Path("manifest.json"))
    stage_parser.add_argument("--output", type=pathlib.Path, required=True)
    stage_parser.add_argument("--extra", action="append", default=[])
    finalize_parser = sub.add_parser("finalize")
    finalize_parser.add_argument("--stage", type=pathlib.Path, required=True)
    finalize_parser.add_argument("--dataset", default=DEFAULT_DATASET)
    finalize_parser.add_argument("--revision", required=True)
    verify_parser = sub.add_parser("verify-remote")
    verify_parser.add_argument("--pointer-url", default=f"https://huggingface.co/datasets/{DEFAULT_DATASET}/resolve/main/{POINTER_PATH}")
    verify_parser.add_argument("--expected-revision")
    verify_parser.add_argument("--require-artifact", action="append", default=[])
    publish_parser = sub.add_parser("publish")
    publish_parser.add_argument("--stage", type=pathlib.Path, required=True)
    publish_parser.add_argument("--dataset", default=DEFAULT_DATASET)
    try:
        args = parser.parse_args()
        if args.command == "stage":
            result = stage(args.manifest, args.output, args.extra)
        elif args.command == "finalize":
            result = finalize(args.stage, args.dataset, args.revision)
        elif args.command == "verify-remote":
            result = verify_remote(args.pointer_url, args.expected_revision, args.require_artifact)
        else:
            result = publish(args.stage, args.dataset, os.environ.get("HF_TOKEN", ""))
    except DistributionError as exc:
        print(json.dumps({"status": "error", "error": str(exc)}), file=sys.stderr)
        return 1
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
