#!/usr/bin/env python3
"""Validate the scheduled freshness import workflow security contract."""

from __future__ import annotations

import pathlib
import sys


WORKFLOW = pathlib.Path(".github/workflows/runtime-freshness-import.yml")
HANDOFF = pathlib.Path("scripts/runtime-freshness-pr-handoff.py")


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
            "current main checkout": "ref: main",
            "producer ancestor guard": "git merge-base --is-ancestor \"${PRODUCER_HEAD_SHA}\" HEAD",
            "serialized imports": "group: runtime-freshness-import",
            "sanitized verification": ".datapan/runtime-freshness/import/verification.json",
            "run receipt": ".datapan/runtime-freshness/import/run-receipt.json",
            "raw report exclusion": "test ! -e .datapan/runtime-freshness/import/raw-combined",
            "transaction": "scripts/apply-runtime-freshness-import.py",
            "producer repository binding": "--producer-repository \"${PRODUCER_REPOSITORY}\"",
            "producer revision binding": "--producer-revision \"${PRODUCER_HEAD_SHA}\"",
            "producer run URL binding": "--producer-run-url \"${PRODUCER_RUN_URL}\"",
            "durable admission": "reports/runtime-freshness-import-admissions",
            "provenance pin commit": "git add README.md docs fixtures manifest.json reports schemas",
            "transaction pipefail": "set -o pipefail",
            "no-change gate": "steps.transaction.outputs.changed == 'true'",
            "bytecode disabled": "PYTHONDONTWRITEBYTECODE: \"1\"",
            "cache cleanup": "-name __pycache__ -prune -exec rm -rf {} +",
            "shared policy-aware handoff": "scripts/runtime-freshness-pr-handoff.py",
            "import attestation resume event": "--resume-event runtime-freshness-import-attest",
            "run-bound handoff": "--run-id \"${PRODUCER_RUN_ID}\"",
            "PR-bound handoff": "--pr \"${pr}\"",
        }
        missing = [label for label, marker in required.items() if marker not in text]
        if missing:
            raise ValueError(f"missing workflow contract markers: {', '.join(missing)}")
        if "secrets." in text:
            raise ValueError("freshness import workflow must not consume repository credential secrets")
        if "ref: ${{ env.PRODUCER_HEAD_SHA }}" in text:
            raise ValueError("freshness import must mutate current main, not a stale producer checkout")
        if "gh pr merge " in text or '"/dispatches"' in text or "for _ in $(seq 1 180)" in text:
            raise ValueError("freshness import must delegate merge and dispatch policy to the shared handoff")
        handoff = HANDOFF.read_text(encoding="utf-8")
        handoff_required = {
            "repository auto-merge policy": ".allow_auto_merge",
            "pending open PR outcome": 'return "pending"',
            "only auto-squash merge request": '"--auto", "--squash"',
            "merged-only next-phase dispatch": "if merged:",
            "manual resume command": "resume_command(repo, run_id, pr, event)",
            "final delivery remains incomplete": "evidence delivery is still pending",
        }
        missing_handoff = [label for label, marker in handoff_required.items() if marker not in handoff]
        if missing_handoff:
            raise ValueError(f"missing shared handoff contract markers: {', '.join(missing_handoff)}")
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
