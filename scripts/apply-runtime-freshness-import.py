#!/usr/bin/env python3
"""Apply a freshness import transactionally through an isolated Git worktree."""

from __future__ import annotations

import argparse
import json
import hashlib
import os
import pathlib
import shlex
import shutil
import subprocess
import sys
import tempfile
from typing import Any

import jsonschema


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
    worktree: pathlib.Path,
    report: pathlib.Path,
    run_receipt: pathlib.Path,
    datapan_command: str,
    import_receipt: pathlib.Path,
    admission: pathlib.Path,
    producer: dict[str, str],
) -> dict[str, Any]:
    python = sys.executable
    proposal = pathlib.Path(".datapan/runtime-freshness/import-proposal.json")
    execute([
        python, "scripts/import-runtime-freshness-run.py",
        "--report", str(report),
        "--receipt", str(run_receipt),
        "--datapan-command", datapan_command,
        "--proposal-output", proposal.as_posix(),
        "--apply",
    ], cwd=worktree)
    execute([python, "scripts/project-runtime-freshness-recovery.py", "--report", str(report), "--run-receipt", str(run_receipt), "--receipt-output", import_receipt.as_posix()], cwd=worktree)
    admission_command = [
        python, "scripts/generate-runtime-freshness-import-admission.py",
        "--root", ".",
        "--proposal", proposal.as_posix(),
        "--report", str(report),
        "--receipt", str(run_receipt),
        "--import-receipt", import_receipt.as_posix(),
        "--producer-repository", producer["repository"],
        "--producer-revision", producer["revision"],
        "--producer-run-id", producer["run_id"],
        "--producer-run-url", producer["run_url"],
        "--output", admission.as_posix(),
    ]
    execute(admission_command, cwd=worktree)
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
        execute(admission_command + ["--check"], cwd=worktree)
    finally:
        pointer.write_bytes(pointer_bytes)
    for cache in worktree.rglob("__pycache__"):
        shutil.rmtree(cache)
    return load(worktree / proposal)


def validate_replay(
    *,
    worktree: pathlib.Path,
    report: pathlib.Path,
    run_receipt: pathlib.Path,
    import_receipt: pathlib.Path,
    admission: pathlib.Path,
    producer: dict[str, str],
) -> dict[str, Any] | None:
    path = worktree / admission
    if not path.exists():
        return None
    value = load(path)
    schema = load(worktree / "schemas/datapan.runtime-freshness-import-admission.v1.schema.json")
    jsonschema.Draft202012Validator(schema, format_checker=jsonschema.FormatChecker()).validate(value)
    inputs = value.get("inputs")
    admitted_producer = value.get("producer")
    if not isinstance(inputs, dict) or not isinstance(admitted_producer, dict):
        raise ValueError("existing admission is malformed")
    expected = {
        "repository": producer["repository"],
        "revision": producer["revision"],
        "run_id": producer["run_id"],
        "run_url": producer["run_url"],
    }
    if value.get("run_id") != producer["run_id"] or admitted_producer != expected:
        raise ValueError("run id already admitted with different producer identity")
    if (
        inputs.get("sanitized_report_sha256") != hashlib.sha256(report.read_bytes()).hexdigest()
        or inputs.get("run_receipt_sha256") != hashlib.sha256(run_receipt.read_bytes()).hexdigest()
    ):
        raise ValueError("run id already admitted with different artifact bytes")
    linked = inputs.get("import_receipt")
    if not isinstance(linked, dict) or linked.get("path") != import_receipt.as_posix():
        raise ValueError("existing admission import receipt path is stale")
    linked_path = worktree / import_receipt
    if not linked_path.is_file() or linked.get("sha256") != hashlib.sha256(linked_path.read_bytes()).hexdigest():
        raise ValueError("existing admission import receipt digest is stale")
    return value


def apply_transaction(
    root: pathlib.Path,
    report: pathlib.Path,
    run_receipt: pathlib.Path,
    datapan_command: str,
    import_receipt: pathlib.Path,
    admission: pathlib.Path,
    producer: dict[str, str],
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
            existing = validate_replay(
                worktree=worktree,
                report=report,
                run_receipt=run_receipt,
                import_receipt=import_receipt,
                admission=admission,
                producer=producer,
            )
            if existing is not None:
                return {
                    "status": "no_change",
                    "run_id": existing["run_id"],
                    "outcome": existing["outcome"],
                    "admission_sha256": hashlib.sha256((worktree / admission).read_bytes()).hexdigest(),
                    "changed_files": [],
                }
            proposal = run_pipeline(worktree, report, run_receipt, datapan_command, import_receipt, admission, producer)
            execute(["git", "add", "-N", "."], cwd=worktree)
            patch = execute(["git", "diff", "--binary", "--no-ext-diff", "HEAD"], cwd=worktree, capture=True)
            if not patch:
                raise ValueError("new run produced no durable admission change")
            changed = [line for line in execute(["git", "diff", "--name-only", "HEAD"], cwd=worktree, capture=True).splitlines() if line]
            if admission.as_posix() not in changed:
                raise ValueError("transaction patch does not contain the run admission")
            patch_path = pathlib.Path(directory) / "import.patch"
            patch_path.write_text(patch, encoding="utf-8")
            execute(["git", "apply", "--check", str(patch_path)], cwd=root)
            execute(["git", "apply", str(patch_path)], cwd=root)
            return {
                "status": "applied",
                "run_id": producer["run_id"],
                "outcome": "imported" if proposal["selected_new_results"] else "no_change",
                "admission_sha256": hashlib.sha256((worktree / admission).read_bytes()).hexdigest(),
                "changed_files": changed,
            }
        finally:
            execute(["git", "-c", "core.hooksPath=/dev/null", "worktree", "remove", "--force", str(worktree)], cwd=root)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--report", type=pathlib.Path, required=True)
    parser.add_argument("--run-receipt", type=pathlib.Path, required=True)
    parser.add_argument("--datapan-command", default="datapan")
    parser.add_argument("--import-receipt", type=pathlib.Path)
    parser.add_argument("--admission", type=pathlib.Path)
    parser.add_argument("--producer-repository", required=True)
    parser.add_argument("--producer-revision", required=True)
    parser.add_argument("--producer-run-url", required=True)
    args = parser.parse_args()
    try:
        root = pathlib.Path(execute(["git", "rev-parse", "--show-toplevel"], cwd=pathlib.Path.cwd(), capture=True).strip())
        receipt = load(args.run_receipt)
        run_id = str(receipt.get("run_id", ""))
        output = args.import_receipt or pathlib.Path("reports/runtime-freshness-imports") / f"{run_id}.json"
        admission = args.admission or pathlib.Path("reports/runtime-freshness-import-admissions") / f"{run_id}.json"
        producer = {
            "repository": args.producer_repository,
            "revision": args.producer_revision,
            "run_id": run_id,
            "run_url": args.producer_run_url,
        }
        result = apply_transaction(root, args.report, args.run_receipt, args.datapan_command, output, admission, producer)
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        return 0
    except Exception as exc:  # noqa: BLE001
        print(f"FAIL apply runtime freshness import: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
