#!/usr/bin/env python3
"""Guard and inventory Registry checkouts of the external datapan-cli repository."""

from __future__ import annotations

import argparse
import json
import pathlib
import re
import sys
from typing import Any


ROOT = pathlib.Path(__file__).resolve().parents[1]
DEFAULT_CONTRACT = ROOT / "policy/external-checkout-refs.json"
DEFAULT_WORKFLOWS = ROOT / ".github/workflows"
DEFAULT_REPORT = ROOT / "reports/external-checkout-refs.json"
SHA_RE = re.compile(r"^[0-9a-f]{40}$")
STEP_RE = re.compile(r"^(?P<indent>\s*)-\s+(?:name|uses):")
CHECKOUT_RE = re.compile(r"^\s*uses:\s*actions/checkout@(?P<version>[^\s#]+)")
FIELD_RE = re.compile(r"^\s*(?P<key>repository|ref|path):\s*(?P<value>[^#]+?)\s*$")


class ContractError(ValueError):
    """Raised when an external checkout violates the reviewed contract."""


def load_json(path: pathlib.Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ContractError(f"expected JSON object: {path}")
    return value


def normalized_scalar(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
        return value[1:-1]
    return value


def checkout_steps(path: pathlib.Path) -> list[dict[str, Any]]:
    lines = path.read_text(encoding="utf-8").splitlines()
    starts = [index for index, line in enumerate(lines) if STEP_RE.match(line)]
    results: list[dict[str, Any]] = []
    for position, start in enumerate(starts):
        start_match = STEP_RE.match(lines[start])
        assert start_match is not None
        indent = len(start_match.group("indent"))
        end = len(lines)
        for candidate in starts[position + 1 :]:
            candidate_match = STEP_RE.match(lines[candidate])
            assert candidate_match is not None
            if len(candidate_match.group("indent")) <= indent:
                end = candidate
                break

        block = lines[start:end]
        action = next((match.group("version") for line in block if (match := CHECKOUT_RE.match(line))), None)
        if action is None:
            continue
        fields: dict[str, str] = {}
        for line in block:
            match = FIELD_RE.match(line)
            if match:
                fields[match.group("key")] = normalized_scalar(match.group("value"))
        if "repository" in fields:
            results.append(
                {
                    "line": start + 1,
                    "action": f"actions/checkout@{action}",
                    **fields,
                }
            )
    return results


def build_report(contract_path: pathlib.Path, workflows_dir: pathlib.Path) -> dict[str, Any]:
    contract = load_json(contract_path)
    if contract.get("schema_version") != "datapan.external-checkout-refs.v1":
        raise ContractError("unsupported external checkout contract schema")
    repository = contract.get("repository")
    selected_ref = contract.get("ref")
    if not isinstance(repository, str) or not repository:
        raise ContractError("contract repository must be a non-empty string")
    if contract.get("ref_kind") != "commit_sha" or not isinstance(selected_ref, str) or not SHA_RE.fullmatch(selected_ref):
        raise ContractError("contract ref must be an immutable 40-character lowercase commit SHA")
    update_policy = contract.get("update_policy")
    if not isinstance(update_policy, dict) or update_policy.get("mutable_refs_allowed") is not False:
        raise ContractError("contract must explicitly prohibit mutable refs")

    inventory: list[dict[str, Any]] = []
    errors: list[str] = []
    for workflow in sorted(workflows_dir.glob("*.y*ml")):
        relative = workflow.relative_to(ROOT).as_posix() if workflow.is_relative_to(ROOT) else workflow.as_posix()
        selected: list[dict[str, Any]] = []
        for checkout in checkout_steps(workflow):
            if checkout.get("repository") != repository:
                continue
            ref = checkout.get("ref")
            if ref is None:
                errors.append(f"{relative}:{checkout['line']}: {repository} checkout is missing ref")
            elif ref != selected_ref:
                errors.append(
                    f"{relative}:{checkout['line']}: ref {ref!r} does not match reviewed identity {selected_ref}"
                )
            selected.append(checkout)
        if selected:
            inventory.append({"workflow": relative, "checkouts": selected})

    if not inventory:
        errors.append(f"no actions/checkout steps found for {repository}")
    if errors:
        raise ContractError("\n".join(errors))
    return {
        "schema_version": "datapan.external-checkout-ref-inventory.v1",
        "contract": contract_path.relative_to(ROOT).as_posix() if contract_path.is_relative_to(ROOT) else contract_path.as_posix(),
        "repository": repository,
        "selected_ref": selected_ref,
        "ref_kind": "commit_sha",
        "checkout_count": sum(len(item["checkouts"]) for item in inventory),
        "workflows": inventory,
    }


def render(report: dict[str, Any]) -> str:
    return json.dumps(report, indent=2, sort_keys=False) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--contract", type=pathlib.Path, default=DEFAULT_CONTRACT)
    parser.add_argument("--workflows", type=pathlib.Path, default=DEFAULT_WORKFLOWS)
    parser.add_argument("--output", type=pathlib.Path, default=DEFAULT_REPORT)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    try:
        report = build_report(args.contract, args.workflows)
        expected = render(report)
        if args.check:
            if not args.output.is_file() or args.output.read_text(encoding="utf-8") != expected:
                raise ContractError(f"external checkout inventory is stale: regenerate {args.output}")
        else:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(expected, encoding="utf-8")
        print(
            json.dumps(
                {
                    "status": "ok",
                    "repository": report["repository"],
                    "selected_ref": report["selected_ref"],
                    "checkout_count": report["checkout_count"],
                    "inventory": str(args.output),
                },
                sort_keys=True,
            )
        )
        return 0
    except (ContractError, OSError, json.JSONDecodeError) as exc:
        print(f"FAIL external checkout ref contract: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
