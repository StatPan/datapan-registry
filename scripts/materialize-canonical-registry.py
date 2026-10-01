#!/usr/bin/env python3
"""Materialize the manifest-bound registry from a versioned public mirror."""

from __future__ import annotations

import argparse
import hashlib
import json
import pathlib
import shutil
import sys
import subprocess
import tempfile
import urllib.error
import urllib.request
import urllib.parse
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


def manifest_sha256(path: pathlib.Path) -> str:
    return file_sha256(path)


def preparation_backend(policy: dict[str, Any], registry_path: pathlib.Path) -> dict[str, Any]:
    backend = policy.get("preparation_backend")
    if not isinstance(backend, dict):
        raise IntegrityError("policy.preparation_backend must declare the Git LFS preparation contract")
    if backend.get("provider") != "github-git-lfs":
        raise IntegrityError("preparation backend must use github-git-lfs")
    if backend.get("path") != registry_path.as_posix():
        raise IntegrityError("preparation backend path must match manifest source_registry")
    if backend.get("manifest_binding") != "release-manifest-sha256":
        raise IntegrityError("preparation backend must bind the exact release manifest sha256")
    if backend.get("upload") != "object-oid-only" or backend.get("readback") != "isolated-lfs-storage":
        raise IntegrityError("preparation backend must upload one LFS OID and read it back into isolated storage")
    repository = backend.get("repository")
    if not isinstance(repository, str) or len(repository.split("/")) != 2 or any(not part for part in repository.split("/")):
        raise IntegrityError("preparation backend repository must be OWNER/REPO")
    return backend


def lfs_object_path(storage: pathlib.Path, oid: str) -> pathlib.Path:
    if len(oid) != 64 or any(char not in "0123456789abcdef" for char in oid):
        raise IntegrityError("Git LFS object OID must be a lowercase sha256")
    return storage / "objects" / oid[:2] / oid[2:4] / oid


def parse_lfs_pointer(pointer: bytes) -> tuple[str, int]:
    try:
        lines = pointer.decode("ascii").splitlines()
    except UnicodeDecodeError as exc:
        raise IntegrityError("candidate Git blob is not a Git LFS pointer") from exc
    values = {line.split(" ", 1)[0]: line.split(" ", 1)[1] for line in lines if " " in line}
    if values.get("version") != "https://git-lfs.github.com/spec/v1":
        raise IntegrityError("candidate Git blob is not a supported Git LFS pointer")
    oid_value = values.get("oid", "")
    if not oid_value.startswith("sha256:"):
        raise IntegrityError("candidate Git LFS pointer has no sha256 OID")
    oid = oid_value.removeprefix("sha256:")
    if len(oid) != 64 or any(char not in "0123456789abcdef" for char in oid):
        raise IntegrityError("candidate Git LFS pointer OID is invalid")
    try:
        size = int(values.get("size", ""))
    except ValueError as exc:
        raise IntegrityError("candidate Git LFS pointer size is invalid") from exc
    if size <= 0:
        raise IntegrityError("candidate Git LFS pointer size must be positive")
    return oid, size


def run_git(arguments: list[str], cwd: pathlib.Path) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(["git", *arguments], cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)


def git_output(arguments: list[str], cwd: pathlib.Path, *, availability: bool = False) -> bytes:
    result = run_git(arguments, cwd)
    if result.returncode != 0:
        error = AvailabilityError if availability else IntegrityError
        action = "remote Git LFS operation" if availability else "git command"
        raise error(f"{action} failed (exit {result.returncode}); command output was captured and omitted")
    return result.stdout


def tracked_policy_binding(policy_path: pathlib.Path, repository_root: pathlib.Path, commit: str) -> dict[str, Any]:
    root = repository_root.resolve()
    path = policy_path.resolve()
    if policy_path.is_symlink() or not path.is_file() or not path.is_relative_to(root):
        raise IntegrityError("distribution policy must be a tracked regular file inside the candidate repository")
    relative_path = path.relative_to(root).as_posix()
    working_bytes = path.read_bytes()
    committed_bytes = git_output(["show", f"{commit}:{relative_path}"], root)
    if committed_bytes != working_bytes:
        raise IntegrityError("working distribution policy bytes do not match the exact candidate commit")
    return {
        "policy_path": relative_path,
        "policy_bytes": len(committed_bytes),
        "policy_sha256": hashlib.sha256(committed_bytes).hexdigest(),
    }


def lfs_storage_from_env(repo_root: pathlib.Path) -> pathlib.Path:
    output = git_output(["lfs", "env"], repo_root)
    for line in output.decode("utf-8", errors="replace").splitlines():
        if line.startswith("LfsStorageDir="):
            return pathlib.Path(line.partition("=")[2])
    raise IntegrityError("git lfs env did not report LfsStorageDir")


def repository_from_remote(remote_url: str) -> str:
    if ":" in remote_url and "@" in remote_url.split(":", 1)[0] and not remote_url.startswith(("ssh://", "https://", "http://")):
        host, path = remote_url.split(":", 1)
        host = host.rsplit("@", 1)[-1]
    else:
        parsed = urllib.parse.urlparse(remote_url)
        host, path = parsed.hostname or "", parsed.path.lstrip("/")
    if host.lower() != "github.com":
        raise IntegrityError("Git LFS preparation remote must be hosted on github.com")
    repository = path.removesuffix(".git").strip("/")
    if len(repository.split("/")) != 2 or any(not part for part in repository.split("/")):
        raise IntegrityError("Git LFS preparation remote URL does not identify OWNER/REPO")
    return repository


def github_lfs_materialize(
    policy: dict[str, Any],
    policy_path: pathlib.Path,
    manifest_path: pathlib.Path,
    registry_path: pathlib.Path,
    expected_bytes: int,
    expected_sha256: str,
    destination: pathlib.Path,
    *,
    candidate_commit: str,
    expected_manifest_sha256: str,
    remote: str,
    check_only: bool,
) -> dict[str, Any]:
    repo_root = manifest_path.resolve().parent
    backend = preparation_backend(policy, registry_path)
    actual_manifest_sha256 = manifest_sha256(manifest_path)
    if len(expected_manifest_sha256) != 64 or any(char not in "0123456789abcdef" for char in expected_manifest_sha256):
        raise IntegrityError("expected manifest sha256 must be 64 lowercase hexadecimal characters")
    if actual_manifest_sha256 != expected_manifest_sha256:
        raise IntegrityError("release manifest sha256 does not match the requested preparation identity")
    if len(candidate_commit) != 40 or any(char not in "0123456789abcdef" for char in candidate_commit) or candidate_commit == "0" * 40:
        raise IntegrityError("candidate commit must be a full nonzero immutable SHA")
    if not remote or remote.startswith("-") or any(char.isspace() for char in remote):
        raise IntegrityError("Git remote name is invalid")

    head = git_output(["rev-parse", "HEAD"], repo_root).decode("ascii", errors="replace").strip()
    if head != candidate_commit:
        raise IntegrityError("candidate commit must be the exact checked-out HEAD")
    committed_manifest = git_output(["show", f"{candidate_commit}:manifest.json"], repo_root)
    if hashlib.sha256(committed_manifest).hexdigest() != actual_manifest_sha256:
        raise IntegrityError("checked-out manifest bytes do not match candidate commit manifest")
    policy_identity = tracked_policy_binding(policy_path, repo_root, candidate_commit)
    relative_path = registry_path.as_posix()
    remote_url = git_output(["remote", "get-url", remote], repo_root).decode("utf-8", errors="replace").strip()
    if repository_from_remote(remote_url).lower() != backend["repository"].lower():
        raise IntegrityError("Git LFS remote does not match the declared preparation repository")
    attributes = git_output(["check-attr", "filter", "--", relative_path], repo_root).decode("utf-8", errors="replace").strip()
    if not attributes.endswith("filter: lfs"):
        raise IntegrityError("manifest source_registry is not configured with the Git LFS filter")
    git_output(["cat-file", "-e", f"{candidate_commit}^{{commit}}"], repo_root)
    pointer = git_output(["show", f"{candidate_commit}:{relative_path}"], repo_root)
    pointer_oid, pointer_size = parse_lfs_pointer(pointer)
    if (pointer_oid, pointer_size) != (expected_sha256, expected_bytes):
        raise IntegrityError("candidate Git LFS pointer does not match the exact release manifest artifact")

    with tempfile.TemporaryDirectory(prefix="datapan-lfs-readback-") as raw_storage:
        isolated_storage = pathlib.Path(raw_storage)
        git_output(
            [
                "-c", f"lfs.storage={isolated_storage}", "lfs", "fetch",
                f"--include={relative_path}", "--exclude=", remote, candidate_commit,
            ],
            repo_root,
            availability=True,
        )
        remote_object = lfs_object_path(isolated_storage, pointer_oid)
        validate(remote_object, expected_bytes, expected_sha256)
        if check_only:
            validate(destination, expected_bytes, expected_sha256)
            status = "verified_remote_readback"
        else:
            destination.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(dir=destination.parent, prefix=f".{destination.name}.", delete=False) as handle:
                temporary = pathlib.Path(handle.name)
            try:
                shutil.copyfile(remote_object, temporary)
                validate(temporary, expected_bytes, expected_sha256)
                temporary.replace(destination)
            finally:
                temporary.unlink(missing_ok=True)
            status = "materialized_after_remote_readback"
    return {
        "status": status,
        "source": "github_git_lfs",
        "backend": backend["provider"],
        "repository": backend["repository"],
        "remote": remote,
        "candidate_commit": candidate_commit,
        "manifest_sha256": actual_manifest_sha256,
        **policy_identity,
        "path": str(destination),
        "bytes": expected_bytes,
        "sha256": expected_sha256,
        "lfs_oid": pointer_oid,
        "readback": "isolated_lfs_storage_verified",
    }


def download(url: str, destination: pathlib.Path) -> None:
    request = urllib.request.Request(url, headers={"User-Agent": "datapan-registry-materializer/1"})
    try:
        with urllib.request.urlopen(request, timeout=120) as response, destination.open("wb") as output:
            shutil.copyfileobj(response, output, length=1024 * 1024)
    except (OSError, urllib.error.URLError) as exc:
        raise AvailabilityError(f"public registry mirror unavailable: {exc}") from exc


def materialize(
    policy_path: pathlib.Path,
    manifest_path: pathlib.Path,
    output: pathlib.Path | None,
    *,
    backend: str = "auto",
    candidate_commit: str | None = None,
    expected_manifest_sha256: str | None = None,
    remote: str = "origin",
    check_only: bool = False,
) -> dict[str, Any]:
    manifest, policy = load_object(manifest_path), load_object(policy_path)
    registry_path, expected_bytes, expected_sha256 = registry_identity(manifest)
    destination = output or registry_path
    if backend == "auto":
        canonical = policy.get("canonical_registry")
        if not isinstance(canonical, dict):
            raise IntegrityError("policy.canonical_registry must be an object")
        backend = "huggingface" if canonical.get("manifest_sha256") == expected_sha256 or not isinstance(policy.get("preparation_backend"), dict) else "github-git-lfs"
    if backend == "github-git-lfs":
        repo_root = manifest_path.resolve().parent
        candidate_commit = candidate_commit or git_output(["rev-parse", "HEAD"], repo_root).decode("ascii", errors="replace").strip()
        expected_manifest_sha256 = expected_manifest_sha256 or manifest_sha256(manifest_path)
        return github_lfs_materialize(
            policy,
            policy_path,
            manifest_path,
            registry_path,
            expected_bytes,
            expected_sha256,
            destination,
            candidate_commit=candidate_commit,
            expected_manifest_sha256=expected_manifest_sha256,
            remote=remote,
            check_only=check_only,
        )
    if backend != "huggingface":
        raise IntegrityError(f"unsupported materialization backend: {backend}")
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
    parser.add_argument("--backend", choices=("auto", "huggingface", "github-git-lfs"), default="auto")
    parser.add_argument("--candidate-commit", help="candidate commit containing the manifest-bound Git LFS pointer")
    parser.add_argument("--expected-manifest-sha256", help="exact sha256 of manifest.json for Git LFS preparation")
    parser.add_argument("--remote", default="origin", help="Git remote used for Git LFS preparation")
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    try:
        if args.check:
            selected_backend = args.backend
            if selected_backend == "auto":
                manifest, policy = load_object(args.manifest), load_object(args.policy)
                _path, _size, digest = registry_identity(manifest)
                canonical = policy.get("canonical_registry")
                selected_backend = "huggingface" if isinstance(canonical, dict) and (canonical.get("manifest_sha256") == digest or not isinstance(policy.get("preparation_backend"), dict)) else "github-git-lfs"
            if selected_backend == "github-git-lfs":
                result = materialize(args.policy, args.manifest, args.output, backend=selected_backend,
                    candidate_commit=args.candidate_commit, expected_manifest_sha256=args.expected_manifest_sha256,
                    remote=args.remote, check_only=True)
                print(json.dumps(result, sort_keys=True))
                return 0
            manifest, policy = load_object(args.manifest), load_object(args.policy)
            registry_path, size, digest = registry_identity(manifest)
            mirror_url(policy, registry_path, digest)
            validate(args.output or registry_path, size, digest)
            result = {"status": "verified", "path": str(args.output or registry_path), "sha256": digest}
        else:
            result = materialize(
                args.policy,
                args.manifest,
                args.output,
                backend=args.backend,
                candidate_commit=args.candidate_commit,
                expected_manifest_sha256=args.expected_manifest_sha256,
                remote=args.remote,
            )
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
