#!/usr/bin/env python3
"""Advance runtime evidence only after its pull request is merged."""

from __future__ import annotations

import argparse
import json
import os
import pathlib
import re
import subprocess
import sys
import time
from typing import Callable


RESUME_EVENTS = {
    "runtime-freshness-import-attest",
    "runtime-freshness-import-attestation-verify",
}


class HandoffError(RuntimeError):
    """A PR handoff could not be advanced safely."""


def gh(args: list[str], *, input_text: str | None = None) -> str:
    """Run one GitHub CLI command and preserve failures as failures."""
    try:
        result = subprocess.run(
            ["gh", *args],
            input=input_text,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
    except OSError as exc:
        raise HandoffError(f"could not run gh: {exc}") from exc
    if result.returncode:
        detail = " ".join(result.stderr.split()) or f"exit code {result.returncode}"
        raise HandoffError(f"gh {' '.join(args)} failed: {detail}")
    return result.stdout.strip()


def repository_auto_merge_allowed(repo: str) -> bool:
    value = gh(["api", f"repos/{repo}", "--jq", ".allow_auto_merge"])
    if value not in {"true", "false"}:
        raise HandoffError(f"repository auto-merge policy was not boolean: {value!r}")
    return value == "true"


def pull_request_state(repo: str, pr: str) -> tuple[str, bool]:
    output = gh([
        "api",
        f"repos/{repo}/pulls/{pr}",
        "--jq",
        "{state: .state, merged: (.merged_at != null)}",
    ])
    try:
        value = json.loads(output)
    except json.JSONDecodeError as exc:
        raise HandoffError(f"pull request API returned invalid state JSON: {output!r}") from exc
    if not isinstance(value, dict):
        raise HandoffError("pull request API state must be a JSON object")
    state = value.get("state")
    merged = value.get("merged")
    if state not in {"open", "closed"} or not isinstance(merged, bool):
        raise HandoffError(f"pull request API returned invalid state: {value!r}")
    if state == "open" and merged:
        raise HandoffError("pull request API returned an open but merged state")
    return state, merged


def dispatch(repo: str, run_id: str, pr: str, event: str) -> None:
    payload = json.dumps(
        {"event_type": event, "client_payload": {"run_id": run_id, "pr_number": pr}},
        separators=(",", ":"),
    )
    gh(["api", "--method", "POST", f"repos/{repo}/dispatches", "--input", "-"], input_text=payload)


def resume_command(repo: str, run_id: str, pr: str, event: str) -> str:
    payload = json.dumps(
        {"event_type": event, "client_payload": {"run_id": run_id, "pr_number": pr}},
        indent=2,
    )
    return (
        f'gh api --method POST "repos/{repo}/dispatches" --input - <<\'JSON\'\n'
        f"{payload}\nJSON"
    )


def summary_text(
    *, repo: str, run_id: str, pr: str, event: str, status: str, reason: str
) -> str:
    if status == "pending":
        return (
            "## Runtime evidence handoff pending\n\n"
            f"Run `{run_id}` is waiting on PR [#{pr}](https://github.com/{repo}/pull/{pr}). "
            f"{reason} No follow-up dispatch was sent. The evidence delivery is still pending "
            "and must not be reported as complete. After this PR is merged, resume this exact run with:\n\n"
            f"```sh\n{resume_command(repo, run_id, pr, event)}\n```\n"
        )
    if status == "failed":
        return (
            "## Runtime evidence handoff failed\n\n"
            f"Run `{run_id}` could not safely advance PR [#{pr}](https://github.com/{repo}/pull/{pr}). "
            f"{reason} No follow-up dispatch was sent.\n"
        )
    return (
        "## Runtime evidence handoff advanced\n\n"
        f"Run `{run_id}` confirmed PR [#{pr}](https://github.com/{repo}/pull/{pr}) is merged "
        f"and dispatched `{event}`. This is a phase handoff; final main verification remains required.\n"
    )


def persist_outcome(
    *, repo: str, run_id: str, pr: str, event: str, status: str, reason: str = ""
) -> dict[str, str]:
    reason = " ".join(reason.split())
    result = {
        "status": status,
        "run_id": run_id,
        "pr_number": pr,
        "resume_event": event,
        "reason": reason,
    }
    if status == "pending":
        result["resume_command"] = resume_command(repo, run_id, pr, event)
    output_path = os.environ.get("GITHUB_OUTPUT")
    if output_path:
        with pathlib.Path(output_path).open("a", encoding="utf-8") as stream:
            for key, value in result.items():
                if key == "resume_command":
                    continue
                if "\n" in value or "\r" in value:
                    raise HandoffError(f"unsafe newline in workflow output {key}")
                stream.write(f"{key}={value}\n")
    summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary_path:
        with pathlib.Path(summary_path).open("a", encoding="utf-8") as stream:
            stream.write(
                summary_text(
                    repo=repo,
                    run_id=run_id,
                    pr=pr,
                    event=event,
                    status=status,
                    reason=reason,
                )
            )
    return result


def advance(
    *,
    repo: str,
    run_id: str,
    pr: str,
    event: str,
    timeout_seconds: int,
    poll_interval_seconds: int,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> tuple[str, str]:
    """Return (status, reason); dispatch only after observing a merged PR."""
    state, merged = pull_request_state(repo, pr)
    if merged:
        dispatch(repo, run_id, pr, event)
        return "dispatched", ""
    if state == "closed":
        raise HandoffError("pull request is closed without a merge")

    if not repository_auto_merge_allowed(repo):
        return "pending", "Repository auto-merge is disabled; this PR needs a human merge."

    gh(["pr", "merge", pr, "--repo", repo, "--auto", "--squash"])
    deadline = clock() + timeout_seconds
    while True:
        remaining = deadline - clock()
        if remaining <= 0:
            return "pending", "The auto-merge request did not reach MERGED before the wait expired."
        sleep(min(poll_interval_seconds, remaining))
        state, merged = pull_request_state(repo, pr)
        if merged:
            dispatch(repo, run_id, pr, event)
            return "dispatched", ""
        if state == "closed":
            raise HandoffError("pull request closed before merge completed")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", default=os.environ.get("GITHUB_REPOSITORY", ""))
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--pr", required=True)
    parser.add_argument("--resume-event", required=True, choices=sorted(RESUME_EVENTS))
    parser.add_argument("--timeout-seconds", type=int, default=1800)
    parser.add_argument("--poll-interval-seconds", type=int, default=10)
    args = parser.parse_args(argv)
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", args.repo):
        parser.error("--repo must be an OWNER/REPO identifier")
    if not re.fullmatch(r"[0-9]+", args.run_id) or not re.fullmatch(r"[0-9]+", args.pr):
        parser.error("--run-id and --pr must be decimal identifiers")
    if args.timeout_seconds < 0 or args.poll_interval_seconds <= 0:
        parser.error("timeout must be non-negative and poll interval must be positive")
    return args


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        status, reason = advance(
            repo=args.repo,
            run_id=args.run_id,
            pr=args.pr,
            event=args.resume_event,
            timeout_seconds=args.timeout_seconds,
            poll_interval_seconds=args.poll_interval_seconds,
        )
        result = persist_outcome(
            repo=args.repo,
            run_id=args.run_id,
            pr=args.pr,
            event=args.resume_event,
            status=status,
            reason=reason,
        )
        print(json.dumps(result, sort_keys=True))
        return 0
    except HandoffError as exc:
        result = persist_outcome(
            repo=args.repo,
            run_id=args.run_id,
            pr=args.pr,
            event=args.resume_event,
            status="failed",
            reason=str(exc),
        )
        print(json.dumps(result, sort_keys=True))
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
