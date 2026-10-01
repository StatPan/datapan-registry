#!/usr/bin/env python3
"""Manage the narrowly owned Git branch used for catalogue processor state.

The branch is an archive for JSON checkpoints only. Every update compares the
remote branch with the exact SHA observed by the caller; a concurrent edit
causes the workflow to stop and retry from the new state.
"""

from __future__ import annotations

import argparse
import json
import os
import pathlib
import re
import subprocess
import sys
from typing import Sequence


STATE_ROOT = pathlib.PurePosixPath(".datapan/upstream-catalogue-state")
STATE_MARKER = STATE_ROOT / "state-root.json"
STATE_SCHEMA = "datapan.upstream-catalogue-state-root.v1"
SOURCE_ID = "data_go_kr"
MAX_TRACKED_FILES = 4200
MAX_STATE_FILE_BYTES = 2 * 1024 * 1024
GIT_OID_RE = re.compile(r"^[a-f0-9]{40}$")
DIGEST_RE = re.compile(r"^[a-f0-9]{64}$")
BRANCH_RE = re.compile(r"^[A-Za-z0-9._/-]+$")


class StateBranchError(RuntimeError):
    """A state branch is unsafe to read or update."""


def git(repo: pathlib.Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        ["git", "-C", str(repo), *args], text=True, capture_output=True,
    )
    if check and result.returncode:
        detail = result.stderr.strip() or result.stdout.strip() or "git command failed"
        raise StateBranchError(f"git_{args[0].replace('-', '_')}_failed: {detail}")
    return result


def state_marker(repository: str) -> dict[str, str]:
    return {
        "schema_version": STATE_SCHEMA,
        "repository": repository,
        "source_id": SOURCE_ID,
        "state_root": STATE_ROOT.as_posix(),
    }


def validate_branch_name(branch: str) -> None:
    if not BRANCH_RE.fullmatch(branch) or branch.startswith("-") or ".." in branch or branch.endswith("/"):
        raise StateBranchError("invalid_state_branch_name")


def owned_state_path(path: str) -> bool:
    relative = pathlib.PurePosixPath(path).relative_to(STATE_ROOT)
    return (
        relative.as_posix() == "state-root.json"
        or relative.as_posix() == f"sources/{SOURCE_ID}/index.json"
        or bool(re.fullmatch(rf"sources/{SOURCE_ID}/generations/{DIGEST_RE.pattern[1:-1]}\.json", relative.as_posix()))
        or bool(re.fullmatch(rf"quarantine/{DIGEST_RE.pattern[1:-1]}\.json", relative.as_posix()))
    )


def remote_sha(repo: pathlib.Path, remote: str, branch: str) -> str | None:
    result = git(repo, "ls-remote", "--heads", remote, f"refs/heads/{branch}")
    rows = [line.split() for line in result.stdout.splitlines() if line.strip()]
    if not rows:
        return None
    if len(rows) != 1 or len(rows[0]) != 2 or rows[0][1] != f"refs/heads/{branch}":
        raise StateBranchError("ambiguous_state_branch_remote_ref")
    sha = rows[0][0]
    if not GIT_OID_RE.fullmatch(sha):
        raise StateBranchError("invalid_state_branch_remote_sha")
    return sha


def validate_tree(repo: pathlib.Path, repository: str, revision: str = "HEAD") -> None:
    raw = git(repo, "ls-tree", "-r", "-z", "--full-tree", revision).stdout
    entries: list[tuple[str, str, str, str]] = []
    for item in raw.encode("utf-8", errors="surrogateescape").split(b"\0"):
        if not item:
            continue
        metadata, raw_path = item.split(b"\t", 1)
        mode, kind, object_id = metadata.decode("ascii").split(" ", 2)
        path = raw_path.decode("utf-8", errors="surrogateescape")
        entries.append((mode, kind, object_id, path))
    if not entries or len(entries) > MAX_TRACKED_FILES:
        raise StateBranchError("state_branch_empty_or_over_capacity")
    for mode, kind, _object_id, path in entries:
        if not path.startswith(f"{STATE_ROOT.as_posix()}/"):
            raise StateBranchError("state_branch_contains_path_outside_owned_root")
        if mode != "100644" or kind != "blob":
            raise StateBranchError("state_branch_contains_non_regular_file")
        if not owned_state_path(path):
            raise StateBranchError("state_branch_contains_unrecognized_archive_path")
    marker_result = git(repo, "show", f"{revision}:{STATE_MARKER.as_posix()}", check=False)
    if marker_result.returncode:
        raise StateBranchError("state_branch_marker_missing")
    try:
        marker = json.loads(marker_result.stdout)
    except json.JSONDecodeError as exc:
        raise StateBranchError("state_branch_marker_invalid_json") from exc
    if marker != state_marker(repository):
        raise StateBranchError("state_branch_marker_identity_mismatch")
    for _mode, _kind, object_id, path in entries:
        size_result = git(repo, "cat-file", "-s", object_id)
        if int(size_result.stdout.strip()) > MAX_STATE_FILE_BYTES:
            raise StateBranchError(f"state_branch_file_over_capacity:{path}")


def prepare(args: argparse.Namespace) -> dict[str, str]:
    source_repo = args.source_repo.resolve()
    worktree = args.worktree.resolve()
    validate_branch_name(args.branch)
    if source_repo == worktree or source_repo in worktree.parents:
        raise StateBranchError("state_worktree_must_be_outside_source_checkout")
    if worktree.exists():
        raise StateBranchError("state_worktree_path_already_exists")
    worktree.parent.mkdir(parents=True, exist_ok=True)
    observed_sha = remote_sha(source_repo, args.remote, args.branch)
    git(source_repo, "worktree", "add", "--detach", str(worktree), "HEAD")
    try:
        if observed_sha:
            git(worktree, "fetch", "--no-tags", args.remote, f"refs/heads/{args.branch}")
            fetched_sha = git(worktree, "rev-parse", "FETCH_HEAD").stdout.strip()
            if fetched_sha != observed_sha:
                raise StateBranchError("state_branch_moved_during_fetch")
            git(worktree, "checkout", "--detach", observed_sha)
            validate_tree(worktree, args.repository)
        else:
            git(worktree, "switch", "--orphan", args.branch)
            git(worktree, "rm", "-rf", "--ignore-unmatch", ".")
            git(worktree, "clean", "-fdx")
            marker_path = worktree / STATE_MARKER
            marker_path.parent.mkdir(parents=True, exist_ok=True)
            marker_path.write_text(
                json.dumps(state_marker(args.repository), sort_keys=True, separators=(",", ":")) + "\n",
                encoding="utf-8",
            )
        git(worktree, "config", "user.name", "github-actions[bot]")
        git(worktree, "config", "user.email", "41898282+github-actions[bot]@users.noreply.github.com")
    except Exception:
        # The path was required to be absent before this script created it.
        git(source_repo, "worktree", "remove", "--force", str(worktree), check=False)
        raise
    return {
        "branch": args.branch,
        "old_sha": observed_sha or "",
        "repository": args.repository,
        "state_dir": (worktree / STATE_ROOT).as_posix(),
        "worktree": worktree.as_posix(),
    }


def status_paths(repo: pathlib.Path) -> list[str]:
    raw = git(repo, "status", "--porcelain=v1", "-z", "--untracked-files=all").stdout.encode(
        "utf-8", errors="surrogateescape",
    )
    paths: list[str] = []
    for item in raw.split(b"\0"):
        if not item:
            continue
        if len(item) < 4 or item[2:3] != b" ":
            raise StateBranchError("state_worktree_status_unparseable")
        if b"R" in item[:2] or b"C" in item[:2]:
            raise StateBranchError("state_worktree_rename_detected")
        path = item[3:].decode("utf-8", errors="surrogateescape")
        # Rename/copy status has a second NUL-delimited path. State files are
        # append/replace only, so reject that shape instead of guessing.
        if b" -> " in item[3:]:
            raise StateBranchError("state_worktree_rename_detected")
        paths.append(path)
    return paths


def commit_and_push(args: argparse.Namespace) -> dict[str, str]:
    worktree = args.worktree.resolve()
    validate_branch_name(args.branch)
    expected = args.expected_old_sha
    if expected and not GIT_OID_RE.fullmatch(expected):
        raise StateBranchError("invalid_expected_state_branch_sha")
    if remote_sha(worktree, args.remote, args.branch) != (expected or None):
        raise StateBranchError("state_branch_compare_and_swap_conflict")
    for path in status_paths(worktree):
        if not path.startswith(f"{STATE_ROOT.as_posix()}/"):
            raise StateBranchError("state_worktree_contains_change_outside_owned_root")
    git(worktree, "add", "-A", "--", STATE_ROOT.as_posix())
    changed = git(worktree, "diff", "--cached", "--name-only", "-z").stdout.encode(
        "utf-8", errors="surrogateescape",
    ).split(b"\0")
    for raw_path in changed:
        if raw_path and not raw_path.decode("utf-8", errors="surrogateescape").startswith(f"{STATE_ROOT.as_posix()}/"):
            raise StateBranchError("state_index_contains_change_outside_owned_root")
    staged = git(worktree, "diff", "--cached", "--quiet", check=False).returncode != 0
    head_result = git(worktree, "rev-parse", "HEAD", check=False)
    head_sha = head_result.stdout.strip() if head_result.returncode == 0 else ""
    if not staged:
        if head_sha != (expected or ""):
            raise StateBranchError("state_branch_noop_head_mismatch")
        return {"changed": "false", "new_sha": head_sha, "old_sha": expected}
    validate_staged_tree(worktree, args.repository)
    git(worktree, "commit", "-m", args.message)
    new_sha = git(worktree, "rev-parse", "HEAD").stdout.strip()
    if remote_sha(worktree, args.remote, args.branch) != (expected or None):
        raise StateBranchError("state_branch_compare_and_swap_conflict")
    lease = f"refs/heads/{args.branch}:{expected}"
    git(
        worktree, "push", f"--force-with-lease={lease}", args.remote,
        f"HEAD:refs/heads/{args.branch}",
    )
    if remote_sha(worktree, args.remote, args.branch) != new_sha:
        raise StateBranchError("state_branch_post_push_sha_mismatch")
    return {"changed": "true", "new_sha": new_sha, "old_sha": expected}


def validate_staged_tree(repo: pathlib.Path, repository: str) -> None:
    # Validate the index view without writing a tree or commit first.
    names = git(repo, "diff", "--cached", "--name-only", "-z").stdout.encode(
        "utf-8", errors="surrogateescape",
    ).split(b"\0")
    for raw_name in names:
        if raw_name and not raw_name.decode("utf-8", errors="surrogateescape").startswith(f"{STATE_ROOT.as_posix()}/"):
            raise StateBranchError("state_index_contains_change_outside_owned_root")
    root = repo / STATE_ROOT
    marker_path = root / "state-root.json"
    if not marker_path.is_file() or marker_path.is_symlink():
        raise StateBranchError("state_branch_marker_missing_or_unsafe")
    try:
        marker = json.loads(marker_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise StateBranchError("state_branch_marker_invalid_json") from exc
    if marker != state_marker(repository):
        raise StateBranchError("state_branch_marker_identity_mismatch")
    files = [path for path in root.rglob("*") if path.is_file() or path.is_symlink()]
    if len(files) > MAX_TRACKED_FILES:
        raise StateBranchError("state_branch_empty_or_over_capacity")
    for path in files:
        if path.is_symlink() or path.stat().st_size > MAX_STATE_FILE_BYTES:
            raise StateBranchError("state_worktree_contains_unsafe_or_oversized_file")
        if not owned_state_path(f"{STATE_ROOT.as_posix()}/{path.relative_to(root).as_posix()}"):
            raise StateBranchError("state_branch_contains_unrecognized_archive_path")
    staged_mode = git(repo, "ls-files", "--stage", "-z").stdout.encode(
        "utf-8", errors="surrogateescape",
    ).split(b"\0")
    for item in staged_mode:
        if not item:
            continue
        metadata, raw_path = item.split(b"\t", 1)
        mode = metadata.decode("ascii").split(" ", 1)[0]
        name = raw_path.decode("utf-8", errors="surrogateescape")
        if mode != "100644" or not name.startswith(f"{STATE_ROOT.as_posix()}/") or not owned_state_path(name):
            raise StateBranchError("state_index_contains_unsafe_archive_entry")


def write_result(path: pathlib.Path | None, value: dict[str, str]) -> None:
    rendered = json.dumps(value, sort_keys=True) + "\n"
    if path:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(rendered, encoding="utf-8")
    print(rendered, end="")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    setup = subparsers.add_parser("prepare")
    setup.add_argument("--source-repo", type=pathlib.Path, required=True)
    setup.add_argument("--worktree", type=pathlib.Path, required=True)
    setup.add_argument("--remote", default="origin")
    setup.add_argument("--branch", required=True)
    setup.add_argument("--repository", required=True)
    setup.add_argument("--result", type=pathlib.Path)
    publish = subparsers.add_parser("commit-push")
    publish.add_argument("--worktree", type=pathlib.Path, required=True)
    publish.add_argument("--remote", default="origin")
    publish.add_argument("--branch", required=True)
    publish.add_argument("--repository", required=True)
    publish.add_argument("--expected-old-sha", required=True)
    publish.add_argument("--message", default="Update upstream catalogue processor state")
    publish.add_argument("--result", type=pathlib.Path)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        result = prepare(args) if args.command == "prepare" else commit_and_push(args)
        write_result(args.result, result)
        return 0
    except (OSError, StateBranchError, subprocess.SubprocessError) as exc:
        print(f"FAIL upstream catalogue state branch: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
