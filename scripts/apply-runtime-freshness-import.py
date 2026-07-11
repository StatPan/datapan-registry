#!/usr/bin/env python3
"""Apply a freshness import transactionally through an isolated Git worktree."""

from __future__ import annotations

import argparse
import json
import os
import pathlib
import shlex
import shutil
import subprocess
import sys
import tempfile
from typing import Any


def load(path: pathlib.Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"{path} must contain an object")
    return value


def execute(command: list[str], *, cwd: pathlib.Path, capture: bool = False, env: dict[str, str] | None = None) -> str:
    environment = os.environ.copy()
    if env:
        environment.update(env)
    result = subprocess.run(command, cwd=cwd, check=True, text=True, stdout=subprocess.PIPE if capture else None, env=environment)
    return result.stdout if capture else ""


def require_clean(root: pathlib.Path) -> None:
    status = execute(["git", "status", "--porcelain"], cwd=root, capture=True)
    if status.strip():
        raise ValueError("transaction requires a clean worktree")


def run_pipeline(
    worktree: pathlib.Path, report: pathlib.Path, run_receipt: pathlib.Path,
    datapan_command: str, import_receipt: pathlib.Path,
) -> None:
    python = sys.executable
    execute([python, "scripts/import-runtime-freshness-run.py", "--report", str(report), "--receipt", str(run_receipt), "--datapan-command", datapan_command, "--apply"], cwd=worktree)
    execute([python, "scripts/project-runtime-freshness-recovery.py", "--report", str(report), "--run-receipt", str(run_receipt), "--receipt-output", import_receipt.as_posix()], cwd=worktree)
    execute([python, "scripts/generate-runtime-evidence-growth.py"], cwd=worktree)
    pointer = worktree / "data/data-go-kr.registry.json"
    pointer_bytes = pointer.read_bytes()
    try:
        execute([python, "scripts/materialize-canonical-registry.py"], cwd=worktree)
        for script in (
            "generate-coverage-backlog.py", "generate-operation-materialization-plan.py",
            "generate-institution-api-overview.py", "generate-institution-runtime-plan.py",
        ):
            execute([python, f"scripts/{script}"], cwd=worktree)
        execute([python, "scripts/refresh-release-ledger-evidence.py", "--write"], cwd=worktree)
        execute([python, "scripts/generate-readme-runtime-snapshot.py"], cwd=worktree)
        execute([python, "scripts/refresh-release-ledger-evidence.py", "--check"], cwd=worktree)
        execute([python, "scripts/validate-runtime-evidence-growth.py"], cwd=worktree)
        execute([python, "scripts/project-runtime-freshness-recovery.py", "--report", str(report), "--run-receipt", str(run_receipt), "--receipt-output", import_receipt.as_posix(), "--check"], cwd=worktree)
    finally:
        pointer.write_bytes(pointer_bytes)
    for cache in worktree.rglob("__pycache__"):
        shutil.rmtree(cache)


def apply_transaction(
    root: pathlib.Path, report: pathlib.Path, run_receipt: pathlib.Path,
    datapan_command: str, import_receipt: pathlib.Path,
) -> dict[str, Any]:
    require_clean(root)
    report, run_receipt = report.resolve(), run_receipt.resolve()
    with tempfile.TemporaryDirectory(prefix="datapan-runtime-import-") as directory:
        worktree = pathlib.Path(directory) / "worktree"
        execute(
            ["git", "-c", "core.hooksPath=/dev/null", "worktree", "add", "--detach", str(worktree), "HEAD"],
            cwd=root,
            env={"GIT_LFS_SKIP_SMUDGE": "1"},
        )
        try:
            run_pipeline(worktree, report, run_receipt, datapan_command, import_receipt)
            execute(["git", "add", "-N", "."], cwd=worktree)
            patch = execute(["git", "diff", "--binary", "--no-ext-diff", "HEAD"], cwd=worktree, capture=True)
            if not patch:
                return {"status": "no_change", "run_id": load(run_receipt).get("run_id"), "changed_files": []}
            changed = [line for line in execute(["git", "diff", "--name-only", "HEAD"], cwd=worktree, capture=True).splitlines() if line]
            patch_path = pathlib.Path(directory) / "import.patch"
            patch_path.write_text(patch, encoding="utf-8")
            execute(["git", "apply", "--check", str(patch_path)], cwd=root)
            execute(["git", "apply", str(patch_path)], cwd=root)
            return {"status": "applied", "run_id": load(run_receipt).get("run_id"), "changed_files": changed}
        finally:
            execute(["git", "-c", "core.hooksPath=/dev/null", "worktree", "remove", "--force", str(worktree)], cwd=root)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--report", type=pathlib.Path, required=True)
    parser.add_argument("--run-receipt", type=pathlib.Path, required=True)
    parser.add_argument("--datapan-command", default="datapan")
    parser.add_argument("--import-receipt", type=pathlib.Path)
    args = parser.parse_args()
    try:
        root = pathlib.Path(execute(["git", "rev-parse", "--show-toplevel"], cwd=pathlib.Path.cwd(), capture=True).strip())
        receipt = load(args.run_receipt)
        output = args.import_receipt or pathlib.Path("reports/runtime-freshness-imports") / f"{receipt.get('run_id')}.json"
        result = apply_transaction(root, args.report, args.run_receipt, args.datapan_command, output)
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        return 0
    except Exception as exc:  # noqa: BLE001
        print(f"FAIL apply runtime freshness import: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
