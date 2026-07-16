#!/usr/bin/env python3
"""Bind a Registry publication run to one exact, reachable source commit."""

from __future__ import annotations

import argparse
import hashlib
import json
import pathlib
import re
import subprocess
from typing import Sequence


FULL_SHA = re.compile(r"[0-9a-f]{40}\Z")
SCHEMA_VERSION = "datapan.registry-publication-source-binding.v1"


class BindingError(RuntimeError):
    pass


def git(repo: pathlib.Path, *args: str, input_bytes: bytes | None = None) -> bytes:
    try:
        result = subprocess.run(
            ["git", "-C", str(repo), *args],
            input=input_bytes,
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
    except subprocess.CalledProcessError as exc:
        detail = exc.stderr.decode("utf-8", errors="replace").strip()
        raise BindingError(detail or f"git {' '.join(args)} failed") from exc
    return result.stdout


def require_sha(value: str, label: str) -> str:
    if not FULL_SHA.fullmatch(value) or value == "0" * 40:
        raise BindingError(f"{label} must be a full nonzero lowercase commit SHA")
    return value


def bind(
    repo: pathlib.Path,
    *,
    source_sha: str,
    workflow_sha: str,
    event_name: str,
    git_ref: str,
    repository: str,
) -> dict[str, object]:
    source_sha = require_sha(source_sha, "source_sha")
    workflow_sha = require_sha(workflow_sha, "workflow_sha")
    if repository != "StatPan/datapan-registry":
        raise BindingError("publication repository identity mismatch")
    if event_name not in {"push", "pull_request", "workflow_dispatch"}:
        raise BindingError("unsupported publication workflow event")
    if event_name == "workflow_dispatch" and git_ref != "refs/heads/main":
        raise BindingError("publication dispatch must run from refs/heads/main")

    head = git(repo, "rev-parse", "HEAD").decode().strip()
    if head != workflow_sha:
        raise BindingError("checked-out workflow SHA does not match workflow_sha")
    for value, label in ((workflow_sha, "workflow_sha"), (source_sha, "source_sha")):
        resolved = git(repo, "rev-parse", f"{value}^{{commit}}").decode().strip()
        if resolved != value:
            raise BindingError(f"{label} does not resolve to the exact commit")
    if event_name == "workflow_dispatch":
        try:
            git(repo, "merge-base", "--is-ancestor", source_sha, workflow_sha)
        except BindingError as exc:
            raise BindingError("source_sha is not an ancestor of the dispatch workflow SHA") from exc
    elif source_sha != workflow_sha:
        raise BindingError("non-dispatch validation must bind source_sha to workflow_sha")

    tree_sha = git(repo, "rev-parse", f"{source_sha}^{{tree}}").decode().strip()
    manifest = git(repo, "show", f"{source_sha}:manifest.json")
    return {
        "schema_version": SCHEMA_VERSION,
        "repository": repository,
        "event_name": event_name,
        "git_ref": git_ref,
        "workflow_sha": workflow_sha,
        "source_sha": source_sha,
        "source_tree_sha": tree_sha,
        "manifest_sha256": hashlib.sha256(manifest).hexdigest(),
        "status": "bound",
    }


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", type=pathlib.Path, default=pathlib.Path("."))
    parser.add_argument("--source-sha", required=True)
    parser.add_argument("--workflow-sha", required=True)
    parser.add_argument("--event-name", required=True)
    parser.add_argument("--git-ref", required=True)
    parser.add_argument("--repository", required=True)
    parser.add_argument("--output", type=pathlib.Path, required=True)
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        receipt = bind(
            args.repo,
            source_sha=args.source_sha,
            workflow_sha=args.workflow_sha,
            event_name=args.event_name,
            git_ref=args.git_ref,
            repository=args.repository,
        )
    except BindingError as exc:
        raise SystemExit(str(exc)) from exc
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(receipt, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(receipt, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
