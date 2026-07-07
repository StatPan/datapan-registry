#!/usr/bin/env python3
"""Refresh release-ledger evidence to a bounded fixed point."""

from __future__ import annotations

import argparse
import shlex
import subprocess
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass


DEFAULT_MAX_ITERATIONS = 5


@dataclass(frozen=True)
class Command:
    argv: tuple[str, ...]
    transient: bool = False

    @property
    def label(self) -> str:
        return shlex.join(self.argv)


WRITE_COMMANDS: tuple[Command, ...] = (
    Command(("python3", "scripts/sync-release-schema-artifacts.py", "--write")),
    Command(("python3", "scripts/generate-source-contract-rollup.py")),
    Command(("python3", "scripts/generate-error-action-routing-rollup.py")),
    Command(("python3", "scripts/generate-source-report-inventory.py")),
    Command(("python3", "scripts/generate-source-runtime-readiness.py")),
    Command(("python3", "scripts/generate-source-runtime-remediation-map.py")),
    Command(("python3", "scripts/generate-credential-runtime-evidence-policy.py")),
    Command(("python3", "scripts/generate-credential-runtime-collection-preflight.py")),
    Command(("python3", "scripts/generate-credential-runtime-runner-readiness.py")),
    Command(("python3", "scripts/generate-credential-runtime-receipt-collection-queue.py")),
    Command(("python3", "scripts/generate-credential-runtime-review-handoff.py")),
    Command(("python3", "scripts/generate-credential-runtime-operator-packets.py")),
    Command(("python3", "scripts/generate-credential-runtime-collection-execution-plan.py")),
    Command(("python3", "scripts/generate-credential-runtime-manual-review-acceptance.py")),
    Command(("python3", "scripts/generate-credential-runtime-manual-review-acceptance-packet.py")),
    Command(("python3", "scripts/generate-impact-plan-rollup.py"), transient=True),
    Command(("python3", "scripts/sync-release-manifest-artifacts.py", "--write")),
    Command(("python3", "scripts/generate-release-distribution-footprint.py")),
    Command(("python3", "scripts/generate-release-shard-consumer-proof.py")),
    Command(("python3", "scripts/generate-release-consumer-compatibility.py")),
    Command(("python3", "scripts/generate-release-operational-pressure.py")),
    Command(("python3", "scripts/generate-release-consumer-decision.py")),
    Command(("python3", "scripts/generate-release-goal-finish-preflight.py")),
    Command(("python3", "scripts/generate-release-goal-continuation-queue.py")),
    Command(("python3", "scripts/generate-release-goal-operating-contract.py")),
    Command(("python3", "scripts/generate-release-assembly-receipt.py"), transient=True),
    Command(("python3", "scripts/sync-release-manifest-artifacts.py", "--write")),
)


CHECK_COMMANDS: tuple[Command, ...] = (
    Command(("python3", "scripts/sync-release-schema-artifacts.py", "--check")),
    Command(("python3", "scripts/sync-release-manifest-artifacts.py", "--check")),
    Command(("python3", "scripts/validate-release-ledger-ownership.py")),
    Command(("python3", "scripts/validate-release-ledger-goal-audit.py")),
    Command(("python3", "scripts/generate-source-contract-rollup.py", "--check")),
    Command(("python3", "scripts/generate-error-action-routing-rollup.py", "--check")),
    Command(("python3", "scripts/generate-source-runtime-remediation-map.py", "--check")),
    Command(("python3", "scripts/generate-credential-runtime-evidence-policy.py", "--check")),
    Command(("python3", "scripts/generate-credential-runtime-collection-preflight.py", "--check")),
    Command(("python3", "scripts/generate-credential-runtime-runner-readiness.py", "--check")),
    Command(("python3", "scripts/generate-credential-runtime-receipt-collection-queue.py", "--check")),
    Command(("python3", "scripts/generate-credential-runtime-review-handoff.py", "--check")),
    Command(("python3", "scripts/generate-credential-runtime-operator-packets.py", "--check")),
    Command(("python3", "scripts/generate-credential-runtime-collection-execution-plan.py", "--check")),
    Command(("python3", "scripts/validate-credential-runtime-manual-review-decision.py")),
    Command(("python3", "scripts/generate-credential-runtime-manual-review-acceptance.py", "--check")),
    Command(("python3", "scripts/generate-credential-runtime-manual-review-acceptance-packet.py", "--check")),
    Command(("python3", "scripts/generate-impact-plan-rollup.py", "--check")),
    Command(("python3", "scripts/generate-release-distribution-footprint.py", "--check")),
    Command(("python3", "scripts/generate-release-shard-consumer-proof.py", "--check")),
    Command(("python3", "scripts/generate-release-consumer-compatibility.py", "--check")),
    Command(("python3", "scripts/validate-release-consumer-compatibility.py")),
    Command(("python3", "scripts/generate-release-operational-pressure.py", "--check")),
    Command(("python3", "scripts/generate-release-consumer-decision.py", "--check")),
    Command(("python3", "scripts/generate-release-goal-finish-preflight.py", "--check")),
    Command(("python3", "scripts/generate-release-goal-continuation-queue.py", "--check")),
    Command(("python3", "scripts/generate-release-goal-operating-contract.py", "--check")),
    Command(("python3", "scripts/guard-release-goal-finish.py", "--self-test")),
    Command(("python3", "scripts/generate-release-assembly-receipt.py", "--check")),
)


class ConvergenceError(RuntimeError):
    """Raised when release evidence does not converge within the iteration limit."""


def run_command(command: Command) -> bool:
    print(f"+ {command.label}", flush=True)
    result = subprocess.run(command.argv, check=False)
    if result.returncode == 0:
        return True
    if command.transient:
        print(f"transient failure allowed before fixed-point convergence: {command.label}", file=sys.stderr)
        return False
    raise subprocess.CalledProcessError(result.returncode, command.argv)


def run_commands(commands: Sequence[Command]) -> bool:
    ok = True
    for command in commands:
        ok = run_command(command) and ok
    return ok


def run_check_commands(commands: Sequence[Command]) -> bool:
    for command in commands:
        result = subprocess.run(command.argv, check=False)
        if result.returncode != 0:
            print(f"check not yet converged: {command.label}", file=sys.stderr)
            return False
    return True


def converge(
    *,
    max_iterations: int,
    refresh_once: Callable[[int], bool],
    checks_pass: Callable[[int], bool],
) -> int:
    if max_iterations < 1:
        raise ValueError("max_iterations must be at least 1")
    for iteration in range(1, max_iterations + 1):
        refresh_once(iteration)
        if checks_pass(iteration):
            return iteration
    raise ConvergenceError(f"release ledger evidence did not converge after {max_iterations} iteration(s)")


def refresh(max_iterations: int) -> int:
    def refresh_once(iteration: int) -> bool:
        print(f"== release ledger evidence refresh iteration {iteration} ==", flush=True)
        return run_commands(WRITE_COMMANDS)

    def checks_pass(iteration: int) -> bool:
        print(f"== release ledger evidence convergence check {iteration} ==", flush=True)
        return run_check_commands(CHECK_COMMANDS)

    return converge(max_iterations=max_iterations, refresh_once=refresh_once, checks_pass=checks_pass)


def check() -> None:
    if not run_check_commands(CHECK_COMMANDS):
        raise ConvergenceError("release ledger evidence is stale; run `python3 scripts/refresh-release-ledger-evidence.py --write`")


def self_test() -> None:
    calls: list[str] = []

    def refresh_once(iteration: int) -> bool:
        calls.append(f"refresh:{iteration}")
        return True

    def checks_pass(iteration: int) -> bool:
        calls.append(f"check:{iteration}")
        return iteration >= 3

    converged = converge(max_iterations=5, refresh_once=refresh_once, checks_pass=checks_pass)
    if converged != 3:
        raise ValueError("self-test failed: expected convergence on iteration 3")
    if calls != ["refresh:1", "check:1", "refresh:2", "check:2", "refresh:3", "check:3"]:
        raise ValueError("self-test failed: unexpected convergence call order")

    try:
        converge(max_iterations=2, refresh_once=refresh_once, checks_pass=lambda _iteration: False)
    except ConvergenceError:
        return
    raise ValueError("self-test failed: non-converging refresh was accepted")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--write", action="store_true", help="refresh release evidence until checks converge")
    mode.add_argument("--check", action="store_true", help="fail when release evidence is not converged")
    mode.add_argument("--self-test", action="store_true", help="test the fixed-point convergence loop")
    parser.add_argument("--max-iterations", type=int, default=DEFAULT_MAX_ITERATIONS)
    args = parser.parse_args()

    try:
        if args.self_test:
            self_test()
            print("ok release ledger evidence refresh self-test")
            return 0
        if args.check:
            check()
            print("ok release ledger evidence fixed point")
            return 0
        iterations = refresh(args.max_iterations)
    except Exception as exc:  # noqa: BLE001 - release operators need the failed command/invariant
        print(f"FAIL release ledger evidence refresh: {exc}", file=sys.stderr)
        return 1

    print(f"ok release ledger evidence fixed point (iterations={iterations})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
