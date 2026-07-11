#!/usr/bin/env python3
"""Validate the scheduled freshness import workflow security contract."""

from __future__ import annotations

import pathlib
import sys


WORKFLOW = pathlib.Path(".github/workflows/runtime-freshness-import.yml")


def main() -> int:
    try:
        text = WORKFLOW.read_text(encoding="utf-8")
        required = {
            "workflow_run trigger": "workflow_run:",
            "producer workflow": "Runtime freshness verification",
            "same repository guard": "head_repository.full_name == github.repository",
            "default branch guard": "head_branch == github.event.repository.default_branch",
            "run-bound artifact": "runtime-freshness-${{ github.event.workflow_run.id }}-consolidated",
            "run-bound download": "run-id: ${{ env.PRODUCER_RUN_ID }}",
            "sanitized verification": ".datapan/runtime-freshness/import/verification.json",
            "run receipt": ".datapan/runtime-freshness/import/run-receipt.json",
            "raw report exclusion": "test ! -e .datapan/runtime-freshness/import/raw-combined",
            "transaction": "scripts/apply-runtime-freshness-import.py",
            "transaction pipefail": "set -o pipefail",
            "no-change gate": "steps.transaction.outputs.changed == 'true'",
            "bytecode disabled": "PYTHONDONTWRITEBYTECODE: \"1\"",
            "cache cleanup": "-name __pycache__ -prune -exec rm -rf {} +",
            "auto merge": "gh pr merge \"${pr}\"",
        }
        missing = [label for label, marker in required.items() if marker not in text]
        if missing:
            raise ValueError(f"missing workflow contract markers: {', '.join(missing)}")
        if "secrets." in text:
            raise ValueError("freshness import workflow must not consume repository credential secrets")
        permissions = text.split("permissions:", 1)[1].split("jobs:", 1)[0]
        expected_permissions = {"actions: read", "contents: write", "pull-requests: write"}
        actual_permissions = {line.strip() for line in permissions.splitlines() if line.strip()}
        if actual_permissions != expected_permissions:
            raise ValueError(f"workflow permissions expected {sorted(expected_permissions)}, got {sorted(actual_permissions)}")
        print("ok runtime freshness import workflow (run_bound=true, sanitized_only=true, idempotent_pr=true)")
        return 0
    except Exception as exc:  # noqa: BLE001
        print(f"FAIL runtime freshness import workflow: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
