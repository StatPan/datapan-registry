#!/usr/bin/env python3
"""Validate rotating freshness workflow safety and cadence invariants."""

from __future__ import annotations

import pathlib
import sys


WORKFLOW = pathlib.Path(".github/workflows/runtime-freshness-verify.yml")


def main() -> int:
    try:
        text = WORKFLOW.read_text(encoding="utf-8")
        required = (
            '- cron: "17 21 * * *"',
            "max-parallel: 2",
            "shard: [0, 1, 2, 3, 4, 5, 6, 7]",
            "lfs: false",
            "materialize-canonical-registry.py",
            "generate-runtime-freshness-batch.py",
            '--rotation-seed "${GITHUB_RUN_NUMBER}"',
            '--shard-index "${{ matrix.shard }}"',
            'DATA_GO_KR_SERVICE_KEY: ${{ secrets.DATAPAN_DATA_GO_KR_SERVICE_KEY || secrets.DATA_GO_KR_SERVICE_KEY }}',
            "set +e",
            'echo "${status}" >',
            "if: always()",
            "retention-days: 30",
            "consolidate:",
            "actions/download-artifact@v8",
            "catalog verify merge",
            "consolidate-runtime-freshness-run.py",
            "--sanitized-output .datapan/runtime-freshness/consolidated/verification.json",
            "runtime-freshness-${{ github.run_id }}-consolidated",
        )
        missing = [fragment for fragment in required if fragment not in text]
        if missing:
            raise ValueError(f"workflow missing invariant(s): {missing}")
        forbidden = ("lfs: true", "git push", "gh pr create", "reports/latest-verification.json --output")
        present = [fragment for fragment in forbidden if fragment in text]
        if present:
            raise ValueError(f"workflow crosses staged evidence boundary: {present}")
        print("ok runtime freshness workflow (daily_shards=8, batch_size=100, max_parallel=2)")
        return 0
    except Exception as exc:  # noqa: BLE001
        print(f"FAIL runtime freshness workflow: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
