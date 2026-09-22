#!/usr/bin/env python3
from __future__ import annotations

import pathlib
import sys


WORKFLOW = pathlib.Path(".github/workflows/runtime-freshness-import-attestation.yml")


def main() -> int:
    try:
        text = WORKFLOW.read_text(encoding="utf-8")
        required = {
            "explicit attest dispatch": "runtime-freshness-import-attest",
            "explicit verify dispatch": "runtime-freshness-import-attestation-verify",
            "serialized lifecycle": "group: runtime-freshness-import-attestation",
            "main checkout": "ref: main",
            "import PR API truth": 'pulls/${IMPORT_PR}',
            "attestation PR API truth": 'pulls/${ATTESTATION_PR}',
            "attestation generator": "scripts/attest-runtime-freshness-import.py attest",
            "main verifier": "scripts/attest-runtime-freshness-import.py verify",
            "run admission": "reports/runtime-freshness-import-admissions/${RUN_ID}.json",
            "run attestation": "reports/runtime-freshness-import-attestations/${RUN_ID}.json",
            "actual merge wait": 'test "${state}" = "MERGED"',
            "final verification dispatch": 'event_type:"runtime-freshness-import-attestation-verify"',
        }
        missing = [label for label, marker in required.items() if marker not in text]
        if missing:
            raise ValueError(f"missing attestation workflow markers: {', '.join(missing)}")
        if "pull_request:" in text or "workflow_run:" in text:
            raise ValueError("attestation workflow must use explicit non-recursive repository dispatches")
        if "secrets." in text:
            raise ValueError("attestation workflow must not consume repository secrets")
        permissions = text.split("permissions:", 1)[1].split("concurrency:", 1)[0]
        actual = {line.strip() for line in permissions.splitlines() if line.strip()}
        expected = {"contents: write", "pull-requests: write"}
        if actual != expected:
            raise ValueError(f"workflow permissions expected {sorted(expected)}, got {sorted(actual)}")
        print("ok runtime freshness import attestation workflow (main_verified=true, recursive=false)")
        return 0
    except Exception as exc:  # noqa: BLE001
        print(f"FAIL runtime freshness import attestation workflow: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
