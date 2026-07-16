#!/usr/bin/env python3
"""Guard and inventory Registry checkouts of the external datapan-cli repository."""

from __future__ import annotations

import argparse
import json
import pathlib
import re
import sys
from typing import Any

import yaml
from yaml.constructor import ConstructorError
from yaml.nodes import MappingNode, ScalarNode
from yaml.tokens import AliasToken, AnchorToken


ROOT = pathlib.Path(__file__).resolve().parents[1]
DEFAULT_CONTRACT = ROOT / "policy/external-checkout-refs.json"
DEFAULT_WORKFLOWS = ROOT / ".github/workflows"
DEFAULT_REPORT = ROOT / ".github/external-checkout-refs.inventory.json"
SHA_RE = re.compile(r"^[0-9a-f]{40}$")
CHECKOUT_RE = re.compile(r"^actions/checkout@[^\s]+$", re.IGNORECASE)
EXPRESSION_MARKER = "${{"


class ContractError(ValueError):
    """Raised when a workflow or checkout violates the reviewed contract."""


class UniqueSafeLoader(yaml.SafeLoader):
    """SafeLoader that rejects merge keys and duplicate mapping keys globally."""

    def construct_mapping(self, node: MappingNode, deep: bool = False) -> dict[Any, Any]:
        if not isinstance(node, MappingNode):
            raise ConstructorError(None, None, "expected a mapping node", node.start_mark)
        mapping: dict[Any, Any] = {}
        for key_node, value_node in node.value:
            if key_node.tag == "tag:yaml.org,2002:merge" or (
                isinstance(key_node, ScalarNode) and key_node.value == "<<"
            ):
                raise ConstructorError(
                    "while constructing a mapping",
                    node.start_mark,
                    "YAML merge keys are not allowed in guarded workflows",
                    key_node.start_mark,
                )
            key = self.construct_object(key_node, deep=deep)
            try:
                duplicate = key in mapping
            except TypeError as exc:
                raise ConstructorError(
                    "while constructing a mapping",
                    node.start_mark,
                    "found an unhashable mapping key",
                    key_node.start_mark,
                ) from exc
            if duplicate:
                raise ConstructorError(
                    "while constructing a mapping",
                    node.start_mark,
                    f"found duplicate key {key!r}",
                    key_node.start_mark,
                )
            mapping[key] = self.construct_object(value_node, deep=deep)
        return mapping


UniqueSafeLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG,
    UniqueSafeLoader.construct_mapping,
)


def load_json(path: pathlib.Path) -> dict[str, Any]:
    def unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        value: dict[str, Any] = {}
        for key, item in pairs:
            if key in value:
                raise ContractError(f"duplicate JSON contract key {key!r}: {path}")
            value[key] = item
        return value

    value = json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=unique_object)
    if not isinstance(value, dict):
        raise ContractError(f"expected JSON object: {path}")
    return value


def load_workflow(path: pathlib.Path) -> dict[str, Any]:
    text = path.read_text(encoding="utf-8")
    try:
        for token in yaml.scan(text, Loader=UniqueSafeLoader):
            if isinstance(token, (AnchorToken, AliasToken)):
                raise ContractError(
                    f"{path}:{token.start_mark.line + 1}: YAML anchors and aliases are not allowed in guarded workflows"
                )
        value = yaml.load(text, Loader=UniqueSafeLoader)
    except (yaml.YAMLError, ConstructorError) as exc:
        raise ContractError(f"invalid or ambiguous workflow YAML {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise ContractError(f"workflow must be a mapping: {path}")
    return value


def require_static_string(value: Any, field: str, location: str) -> str:
    if not isinstance(value, str) or not value:
        raise ContractError(f"{location}: {field} must be a non-empty string")
    if EXPRESSION_MARKER in value:
        raise ContractError(f"{location}: dynamic {field} expressions are not allowed")
    return value


def checkout_steps(path: pathlib.Path) -> list[dict[str, Any]]:
    workflow = load_workflow(path)
    jobs = workflow.get("jobs")
    if not isinstance(jobs, dict):
        raise ContractError(f"{path}: jobs must be a mapping")
    results: list[dict[str, Any]] = []
    for job_name, job in jobs.items():
        location = f"{path}:jobs.{job_name}"
        if not isinstance(job, dict):
            raise ContractError(f"{location} must be a mapping")
        steps = job.get("steps", [])
        if not isinstance(steps, list):
            raise ContractError(f"{location}.steps must be a sequence")
        for index, step in enumerate(steps):
            step_location = f"{location}.steps[{index}]"
            if not isinstance(step, dict):
                raise ContractError(f"{step_location} must be a mapping")
            if "uses" not in step:
                continue
            uses = require_static_string(step["uses"], "uses", step_location)
            if not CHECKOUT_RE.fullmatch(uses):
                continue
            inputs = step.get("with", {})
            if not isinstance(inputs, dict):
                raise ContractError(f"{step_location}.with must be a mapping")
            if "repository" not in inputs:
                continue
            repository = require_static_string(inputs["repository"], "repository", step_location)
            checkout: dict[str, Any] = {
                "job": str(job_name),
                "step": index,
                "action": uses,
                "repository": repository,
            }
            for field in ("ref", "path"):
                if field in inputs:
                    checkout[field] = require_static_string(inputs[field], field, step_location)
            results.append(checkout)
    return results


def validate_expectations(contract: dict[str, Any], inventory: list[dict[str, Any]]) -> None:
    expectations = contract.get("inventory_expectations")
    if not isinstance(expectations, dict):
        raise ContractError("contract inventory_expectations must be a mapping")
    expected_count = expectations.get("checkout_count")
    expected_workflows = expectations.get("workflow_checkout_counts")
    actual_count = sum(len(item["checkouts"]) for item in inventory)
    actual_workflows = {item["workflow"]: len(item["checkouts"]) for item in inventory}
    if expected_count != actual_count:
        raise ContractError(f"checkout count drift: expected {expected_count}, found {actual_count}")
    if expected_workflows != actual_workflows:
        raise ContractError(
            f"workflow checkout inventory drift: expected {expected_workflows!r}, found {actual_workflows!r}"
        )


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
    workflow_paths = sorted(set(workflows_dir.glob("*.yml")) | set(workflows_dir.glob("*.yaml")))
    for workflow in workflow_paths:
        relative = workflow.relative_to(ROOT).as_posix() if workflow.is_relative_to(ROOT) else workflow.as_posix()
        selected: list[dict[str, Any]] = []
        try:
            checkouts = checkout_steps(workflow)
        except ContractError as exc:
            errors.append(str(exc))
            continue
        for checkout in checkouts:
            if checkout["repository"].casefold() != repository.casefold():
                continue
            if checkout["repository"] != repository:
                errors.append(
                    f"{relative}:{checkout['job']}[{checkout['step']}]: repository must use canonical spelling {repository!r}"
                )
            ref = checkout.get("ref")
            if ref is None:
                errors.append(f"{relative}:{checkout['job']}[{checkout['step']}]: {repository} checkout is missing ref")
            elif ref != selected_ref:
                errors.append(
                    f"{relative}:{checkout['job']}[{checkout['step']}]: ref {ref!r} does not match reviewed identity {selected_ref}"
                )
            selected.append(checkout)
        if selected:
            inventory.append({"workflow": relative, "checkouts": selected})

    if errors:
        raise ContractError("\n".join(errors))
    validate_expectations(contract, inventory)
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


def check_inventory(output: pathlib.Path, expected: str) -> None:
    if not output.is_file() or output.read_text(encoding="utf-8") != expected:
        raise ContractError(f"external checkout inventory is stale: regenerate {output}")


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
            check_inventory(args.output, expected)
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
