#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import pathlib
import re
import subprocess
import sys
from collections.abc import Callable
from typing import Any

import jsonschema


SCHEMA_VERSION = "datapan.runtime-freshness-import-attestation.v1"
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
COMMIT_RE = re.compile(r"^[0-9a-f]{40}$")
IMPORT_HEAD_PREFIX = "automation/runtime-freshness-"
ATTESTATION_HEAD_PREFIX = "automation/runtime-freshness-attestation-"
DEFAULT_SCHEMA = pathlib.Path("schemas/datapan.runtime-freshness-import-attestation.v1.schema.json")


ADMISSION_SCHEMA = pathlib.Path("schemas/datapan.runtime-freshness-import-admission.v1.schema.json")
COUNT_KEYS = ("total", "verified", "failed", "skipped", "unknown")

def load(path: pathlib.Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"{path} must contain a JSON object")
    return value


def sha256(path: pathlib.Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def relative_path(root: pathlib.Path, path: pathlib.Path) -> str:
    try:
        return path.resolve().relative_to(root.resolve()).as_posix()
    except ValueError as exc:
        raise ValueError(f"{path} must be inside repository root {root}") from exc


def pull_request(event: dict[str, Any], *, expected_head: str, repository: str) -> dict[str, Any]:
    pull = event.get("pull_request")
    if not isinstance(pull, dict):
        raise ValueError("event does not contain a pull request")
    head = pull.get("head")
    base = pull.get("base")
    if not isinstance(head, dict) or not isinstance(base, dict):
        raise ValueError("pull request head or base is missing")
    head_repo = head.get("repo")
    if head.get("ref") != expected_head or not isinstance(head_repo, dict) or head_repo.get("full_name") != repository:
        raise ValueError("pull request head does not match the expected repository branch")
    if base.get("ref") != "main":
        raise ValueError("pull request base is not main")
    if pull.get("state") != "closed" or pull.get("merged") is not True:
        raise ValueError("pull request closed without a merge")
    number = pull.get("number")
    merge_commit = pull.get("merge_commit_sha")
    merged_at = pull.get("merged_at")
    url = pull.get("html_url")
    if not isinstance(number, int) or number <= 0:
        raise ValueError("pull request number is invalid")
    if not isinstance(merge_commit, str) or not COMMIT_RE.fullmatch(merge_commit):
        raise ValueError("pull request merge commit is invalid")
    if not isinstance(merged_at, str) or not merged_at.endswith("Z"):
        raise ValueError("pull request merged_at is invalid")
    if not isinstance(url, str) or not url.startswith(f"https://github.com/{repository}/pull/"):
        raise ValueError("pull request URL is invalid")
    return {
        "number": number,
        "url": url,
        "merge_commit": merge_commit,
        "merged_at": merged_at,
    }


def git_is_ancestor(root: pathlib.Path, commit: str) -> bool:
    result = subprocess.run(
        ["git", "merge-base", "--is-ancestor", commit, "HEAD"],
        cwd=root,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    return result.returncode == 0


def git_blob(root: pathlib.Path, commit: str, path: str) -> bytes:
    return subprocess.run(
        ["git", "show", f"{commit}:{path}"],
        cwd=root,
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    ).stdout


def require_merge_lineage(
    *,
    root: pathlib.Path,
    commit: str,
    path: str,
    is_ancestor: Callable[[pathlib.Path, str], bool] | None = None,
    read_blob: Callable[[pathlib.Path, str, str], bytes] | None = None,
) -> None:
    is_ancestor = is_ancestor or git_is_ancestor
    read_blob = read_blob or git_blob
    if not is_ancestor(root, commit):
        raise ValueError("recorded merge commit is not reachable from main")
    current = root / path
    if not current.is_file():
        raise ValueError(f"main is missing {path}")
    try:
        merged_bytes = read_blob(root, commit, path)
    except subprocess.CalledProcessError as exc:
        raise ValueError(f"recorded merge commit does not contain {path}") from exc
    if merged_bytes != current.read_bytes():
        raise ValueError(f"{path} differs from the recorded merge commit")


def run_fixed_point(root: pathlib.Path) -> None:
    # The committed registry is a Git LFS pointer. Ledger checks rebuild the
    # health observation plan from the manifest-bound canonical bytes.
    subprocess.run(
        [sys.executable, "scripts/materialize-canonical-registry.py"],
        cwd=root,
        check=True,
    )
    subprocess.run(
        [sys.executable, "scripts/refresh-release-ledger-evidence.py", "--check"],
        cwd=root,
        check=True,
    )



def validate_admission_contract(value: dict[str, Any]) -> None:
    run_id = value.get("run_id")
    producer = value.get("producer")
    arithmetic = value.get("arithmetic")
    selected_identity_set = value.get("selected_identity_set")
    if not isinstance(run_id, str) or not run_id:
        raise ValueError("admission run id is missing")
    if not isinstance(producer, dict) or producer.get("run_id") != run_id:
        raise ValueError("admission producer run id does not match")
    if not isinstance(arithmetic, dict) or not isinstance(selected_identity_set, dict):
        raise ValueError("admission arithmetic or selected identity set is missing")
    before, selected, after = (
        arithmetic.get("before"),
        arithmetic.get("selected"),
        arithmetic.get("after"),
    )
    for label, counts in (("before", before), ("selected", selected), ("after", after)):
        if not isinstance(counts, dict) or set(counts) != set(COUNT_KEYS):
            raise ValueError(f"admission {label} counts are incomplete")
        if any(not isinstance(counts[key], int) or counts[key] < 0 for key in COUNT_KEYS):
            raise ValueError(f"admission {label} counts are invalid")
        if counts["total"] != sum(counts[key] for key in COUNT_KEYS[1:]):
            raise ValueError(f"admission {label} status counts do not sum to total")
    if any(after[key] != before[key] + selected[key] for key in COUNT_KEYS):
        raise ValueError("admission after counts do not equal before plus selected")
    if arithmetic.get("selected_new_results") != selected["total"]:
        raise ValueError("admission selected result count is inconsistent")
    if selected_identity_set.get("count") != selected["total"]:
        raise ValueError("admission selected identity count is inconsistent")
    expected_outcome = "imported" if selected["total"] else "no_change"
    if value.get("outcome") != expected_outcome:
        raise ValueError("admission outcome does not match selected results")

def build(
    *,
    root: pathlib.Path,
    admission_path: pathlib.Path,
    event: dict[str, Any],
    manifest_path: pathlib.Path,
    release_ledger_path: pathlib.Path,
) -> dict[str, Any]:
    admission = load(admission_path)
    run_id = admission.get("run_id")
    producer = admission.get("producer")
    if not isinstance(run_id, str) or not isinstance(producer, dict):
        raise ValueError("admission run or producer identity is missing")
    validate_admission_contract(admission)
    repository = producer.get("repository")
    if not isinstance(repository, str):
        raise ValueError("admission producer repository is missing")
    details = pull_request(
        event,
        expected_head=f"{IMPORT_HEAD_PREFIX}{run_id}",
        repository=repository,
    )
    admission_relative = relative_path(root, admission_path)
    require_merge_lineage(root=root, commit=details["merge_commit"], path=admission_relative)

    inputs = admission.get("inputs")
    arithmetic = admission.get("arithmetic")
    selected_identity_set = admission.get("selected_identity_set")
    if not isinstance(inputs, dict) or not isinstance(arithmetic, dict) or not isinstance(selected_identity_set, dict):
        raise ValueError("admission lineage or arithmetic is missing")
    import_receipt = inputs.get("import_receipt")
    if not isinstance(import_receipt, dict):
        raise ValueError("admission import receipt is missing")
    import_receipt_path = root / str(import_receipt.get("path", ""))
    if not import_receipt_path.is_file() or sha256(import_receipt_path) != import_receipt.get("sha256"):
        raise ValueError("admission import receipt is absent or stale")
    if not manifest_path.is_file() or not release_ledger_path.is_file():
        raise ValueError("Registry manifest or release ledger is missing")

    return {
        "schema_version": SCHEMA_VERSION,
        "attested_at": details["merged_at"],
        "run_id": run_id,
        "outcome": admission.get("outcome"),
        "producer": producer,
        "lineage": {
            "admission": {"path": admission_relative, "sha256": sha256(admission_path)},
            "sanitized_report_sha256": inputs.get("sanitized_report_sha256"),
            "run_receipt_sha256": inputs.get("run_receipt_sha256"),
            "import_receipt": import_receipt,
            "selected_identity_set": selected_identity_set,
        },
        "import": {
            "pull_request": details["number"],
            "pull_request_url": details["url"],
            "merge_commit": details["merge_commit"],
            "merged_at": details["merged_at"],
            "arithmetic": arithmetic,
        },
        "registry": {
            "manifest": {"path": relative_path(root, manifest_path), "sha256": sha256(manifest_path)},
            "release_ledger": {"path": relative_path(root, release_ledger_path), "sha256": sha256(release_ledger_path)},
        },
        "verification": {
            "main_ref": "refs/heads/main",
            "merge_commit_reachable": True,
            "admission_matches_merge": True,
            "release_ledger_fixed_point": True,
        },
    }


def validate_schema(value: dict[str, Any], schema_path: pathlib.Path) -> None:
    jsonschema.Draft202012Validator(load(schema_path), format_checker=jsonschema.FormatChecker()).validate(value)


def validate_current(
    *,
    root: pathlib.Path,
    value: dict[str, Any],
    is_ancestor: Callable[[pathlib.Path, str], bool] | None = None,
    read_blob: Callable[[pathlib.Path, str, str], bytes] | None = None,
) -> None:
    lineage = value.get("lineage")
    import_data = value.get("import")
    registry = value.get("registry")
    if not isinstance(lineage, dict) or not isinstance(import_data, dict) or not isinstance(registry, dict):
        raise ValueError("attestation lineage, import, or registry section is missing")
    admission = lineage.get("admission")
    import_receipt = lineage.get("import_receipt")
    if not isinstance(admission, dict) or not isinstance(import_receipt, dict):
        raise ValueError("attestation receipt lineage is missing")
    for label, record in (("admission", admission), ("import receipt", import_receipt)):
        path = root / str(record.get("path", ""))
        if not path.is_file() or sha256(path) != record.get("sha256"):
            raise ValueError(f"{label} is absent or stale")
    admission_path = root / str(admission["path"])
    admission_value = load(admission_path)
    validate_admission_contract(admission_value)
    admission_inputs = admission_value.get("inputs")
    if not isinstance(admission_inputs, dict):
        raise ValueError("admission inputs are missing")
    expected_lineage = {
        "sanitized_report_sha256": admission_inputs.get("sanitized_report_sha256"),
        "run_receipt_sha256": admission_inputs.get("run_receipt_sha256"),
        "import_receipt": admission_inputs.get("import_receipt"),
        "selected_identity_set": admission_value.get("selected_identity_set"),
    }
    for key, expected in expected_lineage.items():
        if lineage.get(key) != expected:
            raise ValueError(f"attestation {key} does not match admission")
    if value.get("run_id") != admission_value.get("run_id"):
        raise ValueError("attestation run id does not match admission")
    if value.get("outcome") != admission_value.get("outcome"):
        raise ValueError("attestation outcome does not match admission")
    if value.get("producer") != admission_value.get("producer"):
        raise ValueError("attestation producer does not match admission")
    if import_data.get("arithmetic") != admission_value.get("arithmetic"):
        raise ValueError("attestation arithmetic does not match admission")
    for label, record in (("manifest", registry.get("manifest")), ("release ledger", registry.get("release_ledger"))):
        if not isinstance(record, dict):
            raise ValueError(f"{label} binding is missing")
        path = root / str(record.get("path", ""))
        if not path.is_file() or sha256(path) != record.get("sha256"):
            raise ValueError(f"{label} digest is stale")
    commit = import_data.get("merge_commit")
    if not isinstance(commit, str):
        raise ValueError("attestation merge commit is missing")
    require_merge_lineage(
        root=root,
        commit=commit,
        path=str(admission["path"]),
        is_ancestor=is_ancestor,
        read_blob=read_blob,
    )


def render(value: dict[str, Any]) -> bytes:
    return (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode()


def write_exact(output: pathlib.Path, content: bytes) -> None:
    if output.exists():
        if output.read_bytes() != content:
            raise ValueError("run id already has different attestation bytes")
        return
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_bytes(content)


def generate(args: argparse.Namespace) -> dict[str, Any]:
    root = args.root.resolve()
    validate_schema(load((root / args.admission).resolve()), root / ADMISSION_SCHEMA)
    run_fixed_point(root)
    value = build(
        root=root,
        admission_path=(root / args.admission).resolve(),
        event=load(args.event),
        manifest_path=(root / args.manifest).resolve(),
        release_ledger_path=(root / args.release_ledger).resolve(),
    )
    validate_schema(value, root / args.schema)
    write_exact(root / args.output, render(value))
    return value


def verify(args: argparse.Namespace) -> dict[str, Any]:
    root = args.root.resolve()
    run_id = args.run_id
    event = load(args.event)
    repository = event.get("repository", {}).get("full_name")
    if not isinstance(repository, str):
        raise ValueError("event repository is missing")
    details = pull_request(
        event,
        expected_head=f"{ATTESTATION_HEAD_PREFIX}{run_id}",
        repository=repository,
    )
    output = root / "reports/runtime-freshness-import-attestations" / f"{run_id}.json"
    if not output.is_file():
        raise ValueError("main is missing the run attestation")
    require_merge_lineage(
        root=root,
        commit=details["merge_commit"],
        path=relative_path(root, output),
    )
    value = load(output)
    if value.get("run_id") != run_id:
        raise ValueError("attestation run id does not match the branch")
    validate_schema(value, root / args.schema)
    lineage = value.get("lineage")
    if not isinstance(lineage, dict) or not isinstance(lineage.get("admission"), dict):
        raise ValueError("attestation admission lineage is missing")
    validate_schema(load(root / str(lineage["admission"]["path"])), root / ADMISSION_SCHEMA)
    run_fixed_point(root)
    validate_current(root=root, value=value)
    return value


def main() -> int:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)
    attest = subparsers.add_parser("attest")
    attest.add_argument("--root", type=pathlib.Path, default=pathlib.Path("."))
    attest.add_argument("--event", required=True, type=pathlib.Path)
    attest.add_argument("--admission", required=True, type=pathlib.Path)
    attest.add_argument("--output", required=True, type=pathlib.Path)
    attest.add_argument("--manifest", type=pathlib.Path, default=pathlib.Path("manifest.json"))
    attest.add_argument("--release-ledger", type=pathlib.Path, default=pathlib.Path("reports/release-assembly-receipt.json"))
    attest.add_argument("--schema", type=pathlib.Path, default=DEFAULT_SCHEMA)
    verify_parser = subparsers.add_parser("verify")
    verify_parser.add_argument("--root", type=pathlib.Path, default=pathlib.Path("."))
    verify_parser.add_argument("--event", required=True, type=pathlib.Path)
    verify_parser.add_argument("--run-id", required=True)
    verify_parser.add_argument("--schema", type=pathlib.Path, default=DEFAULT_SCHEMA)
    args = parser.parse_args()
    try:
        value = generate(args) if args.command == "attest" else verify(args)
        print(json.dumps({"status": "attested" if args.command == "attest" else "verified", "run_id": value["run_id"]}, sort_keys=True))
        return 0
    except Exception as exc:  # noqa: BLE001
        print(f"FAIL runtime freshness import attestation: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
