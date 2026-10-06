#!/usr/bin/env python3
"""Prepare owned, reviewable canonical update PRs from frozen processor runs.

The workflow caller validates the immutable #657 state/artifact handoff,
refreshes source-bound release evidence with the pinned native CLI, uploads
only the candidate LFS object, proves it from an empty storage root, and then
uses Git/API compare-and-swap checks before advancing an owned PR. It never
merges or publishes to Hugging Face.
"""

from __future__ import annotations

import argparse
import copy
import datetime as dt
import hashlib
import importlib.util
import json
import os
import pathlib
import re
import selectors
import shutil
import shlex
import stat
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from collections.abc import Mapping, Sequence
from typing import Any

import yaml

os.environ.setdefault("PYTHONDONTWRITEBYTECODE", "1")

PROCESSOR_SCHEMA = "datapan.upstream-catalogue-checkpoint.v1"
JOURNAL_SCHEMA = "datapan.canonical-update-promotion-journal.v1"
JOURNAL_PATH = pathlib.Path("reports/canonical-update-promotion-receipt.json")
STATE_BRANCH = "automation/canonical-update-state"
PROCESSOR_STATE_BRANCH = "automation/upstream-catalogue-state"
PROCESSOR_STATE_ROOT = pathlib.PurePosixPath(".datapan/upstream-catalogue-state")
PROCESSOR_WORKFLOW_NAME = "Process upstream catalogue"
PROCESSOR_WORKFLOW_PATH = ".github/workflows/upstream-catalogue-process.yml"
PROCESSOR_ARTIFACT_PREFIX = "upstream-catalogue-processing-"
VERIFY_WORKFLOW_PATH = ".github/workflows/verify-release.yml"
GITHUB_API_VERSION = "2026-03-10"
CI_EXPECTATION_UNSET = object()
STATE_EXPECTATION_UNSET = object()
PROCESSING_PREFIX = "upstream-catalogue-processing-"
PROMOTION_ISSUE_MARKER = "datapan-canonical-update-issue:v1:"
REVIEW_DIR = pathlib.Path("reports/data-go-kr/upstream-catalogue-review")
MAX_PROCESSOR_ARCHIVE_BYTES = 1024 * 1024 * 1024
MAX_PROCESSOR_BUNDLE_BYTES = 2 * 1024 * 1024 * 1024
MAX_PROCESSOR_ARTIFACT_PAGES = 10
PROCESSOR_INPUT_PROVENANCE = {
    "policy_sha256": "policy/source-refresh.json",
    "adapter_revision": "data/provider-index.json",
    "generator_revision": "scripts/process-upstream-catalogue-candidate.py",
    "extractor_revision": "scripts/generate-batch-link-detail-registry-patches.py",
}
PROCESSOR_COMPOSITION_INPUTS = {
    "registry_schema": "schemas/datapan.specs.v1.schema.json",
    "provider_index_schema": "schemas/datapan.provider-index.v1.schema.json",
    "diff_schema": "schemas/datapan.catalog-diff.v1.schema.json",
    "refresh_evidence_schema": "schemas/datapan.upstream-refresh-evidence.v1.schema.json",
    "enrichment_evidence_schema": "schemas/datapan.catalogue-enrichment-evidence.v1.schema.json",
    "composer": "scripts/compose-upstream-catalogue-candidate.py",
    "receipt_schema": "schemas/datapan.catalogue-composition-receipt.v1.schema.json",
}
# The last admitted composer before #746 emitted no LINK contract projection.
# Its exact source identity is the only historical encoding accepted below;
# other producer revisions must carry the current complete projection.
HISTORICAL_LINK_CONTRACT_COMPOSER = {
    "bytes": 78840,
    "sha256": "bf55d918b9763512b59c00ae4ff252342ec48b0ee687f46926a34448d5bbdc53",
}
PROCESSOR_COMPOSER_READ_TIMEOUT_SECONDS = 10.0
MAX_PROCESSOR_COMPOSER_BYTES = 1024 * 1024
MAX_PROCESSOR_COMPOSER_STDERR_BYTES = 64 * 1024
PROCESSOR_COMPOSER_TERMINATE_GRACE_SECONDS = 0.25
HISTORICAL_LINK_CONTRACT_REQUIRED_EVIDENCE = [
    "current_link_detail_page",
    "operation_source_provenance",
    "registered_adapter_host",
]
PROCESSOR_COMPATIBILITY_FILES = (
    *PROCESSOR_INPUT_PROVENANCE.values(),
    "contracts/provider-operation-declarations/data-go-kr-15056854-oa-109-search-last-train-time.v1.json",
    "contracts/provider-operation-declarations/data-go-kr-15056854-historical-subject-0085.v1.json",
    ".github/workflows/upstream-catalogue-process.yml",
    ".github/workflows/upstream-catalog-refresh.yml",
    "scripts/upstream-catalogue-state-branch.py",
    "scripts/upstream_catalogue_handoff.py",
    "scripts/seoul_oa109_operation_declaration.py",
    "scripts/generate-seoul-oa109-subject-snapshot.py",
    "scripts/compose-upstream-catalogue-candidate.py",
    "schemas/datapan.upstream-catalogue-checkpoint.v1.schema.json",
    "schemas/datapan.catalogue-composition-receipt.v1.schema.json",
    *PROCESSOR_COMPOSITION_INPUTS.values(),
)
GENERATED_FILE_ALLOWLIST = frozenset({
    "data/data-go-kr.registry.json", "manifest.json", "schemas/index.json",
    "policy/data-go-kr-operation-denominator-expectation.json",
    "reports/catalog-diff.json", "reports/catalog-audit.json", "reports/error-catalog.json",
    "reports/dependencies.json", "reports/adapter-targets.json", "reports/provider-backlog.json",
    "reports/route-disposition.json", "reports/coverage.json", "reports/verification-plan.json",
    "reports/data-go-kr/operation-denominator.json", "reports/data-go-kr/operation-manifest.json",
    "reports/operation-denominator-rollup.json", "reports/current-runtime-evidence-projection.json",
    "reports/runtime-freshness-queue.json", "reports/health-probe-catalog.json",
    "reports/diagnostic-current-source-applicability.json",
    "fixtures/health-probe-catalog/cli-health-probe-v1.json", "reports/data-go-kr/runtime-evidence-growth.json",
    "drafts/operation-assertion-policies/operation-assertion-policies.v1.json",
    "fixtures/operation-assertion-policies/datapan-health-consumer-proof.v1.json",
    "drafts/operation-assertion-policies/release-manifest.v1.json",
    "drafts/operation-assertion-policies/release-candidate.v1.json",
    "drafts/diagnostic-envelope/release-candidate/diagnostic-release-candidate.v1.json",
    "schemas/datapan.diagnostic-envelope.v1.schema.json", "policy/diagnostic-envelope-consumer-contract.v1.json",
    "policy/data-go-kr-diagnostic-evidence-mapping.v1.json", "policy/diagnostic-cause-action-vocabulary.v1.json",
    "reports/diagnostic-publication-readiness.json",
    "reports/diagnostic-consumer-compatibility/datapan-cli.v1.json",
    "reports/diagnostic-consumer-compatibility/datapan-health.v1.json",
    "reports/diagnostic-consumer-compatibility/datapan-web.v1.json",
    "reports/data-go-kr/coverage-backlog.json", "docs/data-go-kr-coverage-backlog.md",
    "reports/data-go-kr/external-adapter-backlog.json", "docs/data-go-kr-external-adapter-backlog.md",
    "reports/data-go-kr/operation-materialization-plan.json", "docs/data-go-kr-operation-materialization-plan.md",
    "reports/data-go-kr/institution-api-overview.json", "docs/data-go-kr-institution-api-overview.md",
    "reports/data-go-kr/institution-runtime-plan.json", "docs/data-go-kr-institution-runtime-plan.md",
    "reports/sustainable-coverage.json", "README.md",
    "reports/source-contract-rollup.json", "reports/error-action-routing-rollup.json",
    "reports/failure-recovery-rollup.json", "reports/source-report-inventory.json",
    "reports/source-runtime-evidence-rollup.json", "docs/source-runtime-readiness.md",
    "reports/source-runtime-remediation-map.json", "reports/credential-runtime-evidence-policy.json",
    "reports/credential-runtime-collection-preflight.json", "reports/credential-runtime-runner-readiness.json",
    "reports/credential-runtime-receipt-collection-queue.json", "reports/credential-runtime-review-handoff.json",
    "reports/credential-runtime-collection-execution-plan.json", "reports/release-consumer-compatibility.json",
    "reports/credential-runtime-manual-review-technical-rebinding.json",
    "reports/credential-runtime-manual-review-acceptance.json",
    "reports/credential-runtime-manual-review-acceptance-packet.json",
    "reports/registry-impact-plan.json", "reports/release-distribution-footprint.json",
    "reports/release-shard-consumer-proof.json", "reports/release-operational-pressure.json",
    "reports/release-consumer-decision.json", "docs/release-ledger-goal-completion-audit.json",
    "reports/release-goal-finish-preflight.json", "reports/release-goal-continuation-queue.json",
    "reports/release-goal-operating-contract.json", "reports/release-assembly-receipt.json",
    "reports/health-runtime-observation-plan.v1.json", "reports/release-version-decision.json",
    "reports/regional-baseline-source-provenance.json", "fixtures/source-provenance/regional-baseline-v0-pin.json",
})
REQUIRED_PROCESSOR_FILES = (
    "composed-candidate.registry.json",
    "ready-scope.registry.json",
    "semantic-diff.json",
    "regeneration-queue.json",
    "quarantine.json",
    "composition-receipt.json",
    "upstream-catalogue-enrichment-evidence.json",
    "upstream-catalogue-processing-result.json",
)


class PromotionError(RuntimeError):
    pass


class ProcessorCandidateError(PromotionError):
    """One retained B generation is unusable; later durable generations may proceed."""


class ProcessorArtifactUnavailable(ProcessorCandidateError):
    pass


def canonical_json(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def file_sha256(path: pathlib.Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def load_object(path: pathlib.Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PromotionError(f"invalid required JSON input: {path}") from exc
    if not isinstance(value, dict):
        raise PromotionError(f"required JSON input must be an object: {path}")
    return value


def load_json_value(path: pathlib.Path) -> Any:
    """Load one required JSON document without imposing an object root."""
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PromotionError(f"invalid required JSON input: {path}") from exc


def load_module(path: pathlib.Path, name: str) -> Any:
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise PromotionError(f"cannot load required helper: {path}")
    module = importlib.util.module_from_spec(spec)
    missing = object()
    previous = sys.modules.get(name, missing)
    # Some helpers define dataclasses (and other decorators) that resolve
    # their module through sys.modules while the class body is executing.
    # Match normal import semantics, and avoid leaving a half-initialized
    # module behind when execution fails.
    sys.modules[name] = module
    try:
        spec.loader.exec_module(module)
    except BaseException:
        if previous is missing:
            sys.modules.pop(name, None)
        else:
            sys.modules[name] = previous
        raise
    return module


def load_canonical_update_pr(root: pathlib.Path) -> Any:
    """Load the shared canonical composition and promotion validator."""
    return load_module(root / "scripts/canonical_update_pr.py", "canonical_update_pr")


def command(
    argv: Sequence[str],
    cwd: pathlib.Path,
    *,
    allowed_returncodes: frozenset[int] = frozenset({0}),
    env: Mapping[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    print(f"+ [{cwd}] {shlex.join(tuple(argv))}", flush=True)
    result = subprocess.run(tuple(argv), cwd=cwd, text=True, capture_output=True, check=False, env=dict(env) if env else None)
    if result.returncode not in allowed_returncodes:
        # Authenticated Git LFS failures can contain signed URLs; report only
        # the command identity and exit code, never provider response text.
        raise PromotionError(f"command failed ({result.returncode}): {shlex.join(tuple(argv))}")
    return result


def processor_state_repository_from_remote(url: str) -> str:
    """Return the owner/repository identity from a GitHub remote URL."""
    value = url.strip()
    if not value:
        raise PromotionError("processor state checkout has no origin remote")
    if value.startswith("git@"):
        host_path = value.removeprefix("git@")
        host, separator, path = host_path.partition(":")
        if not separator or host.casefold() != "github.com":
            raise PromotionError("processor state checkout origin is not the configured GitHub repository")
    else:
        parsed = urllib.parse.urlparse(value)
        if (
            parsed.scheme not in {"https", "ssh"}
            or (parsed.hostname or "").casefold() != "github.com"
            or parsed.username is not None
            or parsed.password is not None
            or parsed.port is not None
            or parsed.query
            or parsed.fragment
        ):
            raise PromotionError("processor state checkout origin is not the configured GitHub repository")
        path = parsed.path.lstrip("/")
    path = path.removesuffix(".git").strip("/")
    parts = path.split("/")
    if len(parts) != 2 or any(not part or part in {".", ".."} for part in parts):
        raise PromotionError("processor state checkout origin has an invalid repository path")
    return "/".join(parts)


def export_processor_state_pin(
    checkout: pathlib.Path,
    processor_state_sha: str,
    repository: str,
    destination: pathlib.Path,
    *,
    command_cwd: pathlib.Path,
) -> tuple[pathlib.Path, dict[str, str]]:
    """Read one exact ancestor state tree without checking out or changing B's branch."""
    if not re.fullmatch(r"[a-f0-9]{40}", processor_state_sha):
        raise PromotionError("processor state pin must be a full immutable Git commit SHA")
    if not repository or "/" not in repository:
        raise PromotionError("configured repository is required for a processor state pin")
    if checkout.is_symlink() or not checkout.is_dir():
        raise PromotionError("processor state checkout is missing or unsafe")
    checkout_root = checkout.resolve(strict=True)
    if destination.exists() or destination.is_symlink():
        raise PromotionError("processor state pin export destination already exists")
    if destination.resolve(strict=False).is_relative_to(checkout_root):
        raise PromotionError("processor state pin export must remain outside the B checkout")
    checkout_top = command(("git", "-C", str(checkout_root), "rev-parse", "--show-toplevel"), command_cwd).stdout.strip()
    if pathlib.Path(checkout_top).resolve() != checkout_root:
        raise PromotionError("processor state checkout root does not match the requested path")
    remote_url = command(("git", "-C", str(checkout_root), "remote", "get-url", "origin"), command_cwd).stdout.strip()
    if processor_state_repository_from_remote(remote_url).casefold() != repository.casefold():
        raise PromotionError("processor state checkout repository differs from GITHUB_REPOSITORY")

    state_ref = f"refs/heads/{PROCESSOR_STATE_BRANCH}"
    remote_rows = [line.split("\t", 1) for line in command(
        ("git", "-C", str(checkout_root), "ls-remote", "--heads", "origin", state_ref), command_cwd,
    ).stdout.splitlines() if line]
    if len(remote_rows) != 1 or len(remote_rows[0]) != 2 or remote_rows[0][1] != state_ref:
        raise PromotionError("authoritative processor state branch is absent or ambiguous")
    remote_head = remote_rows[0][0]
    if not re.fullmatch(r"[a-f0-9]{40}", remote_head):
        raise PromotionError("authoritative processor state branch head is malformed")
    tracking_ref = f"refs/remotes/origin/{PROCESSOR_STATE_BRANCH}"
    tracking_head = command(("git", "-C", str(checkout_root), "rev-parse", "--verify", f"{tracking_ref}^{{commit}}"), command_cwd).stdout.strip()
    checkout_head = command(("git", "-C", str(checkout_root), "rev-parse", "--verify", "HEAD^{commit}"), command_cwd).stdout.strip()
    if tracking_head != remote_head or checkout_head != remote_head:
        raise PromotionError("processor state checkout is stale relative to the authoritative B branch")
    pinned_commit = command(("git", "-C", str(checkout_root), "rev-parse", "--verify", f"{processor_state_sha}^{{commit}}"), command_cwd).stdout.strip()
    if pinned_commit != processor_state_sha:
        raise PromotionError("processor state pin does not resolve to its exact immutable commit")
    ancestry = command(
        ("git", "-C", str(checkout_root), "merge-base", "--is-ancestor", processor_state_sha, remote_head),
        command_cwd, allowed_returncodes=frozenset({0, 1}),
    )
    if ancestry.returncode != 0:
        raise PromotionError("processor state pin is not an ancestor of the authoritative B state branch")

    destination.mkdir(parents=True, exist_ok=False)
    archive_path = destination.parent / "processor-state-pin.tar"
    if archive_path.exists() or archive_path.is_symlink():
        raise PromotionError("processor state pin archive path already exists")
    command((
        "git", "-C", str(checkout_root), "archive", "--format=tar", "--output", str(archive_path),
        processor_state_sha, PROCESSOR_STATE_ROOT.as_posix(),
    ), command_cwd)
    prefix = PROCESSOR_STATE_ROOT.parts
    total_bytes = 0
    seen: set[pathlib.PurePosixPath] = set()
    try:
        with tarfile.open(archive_path, mode="r:") as archive:
            members = archive.getmembers()
            if not members:
                raise PromotionError("processor state pin has no owned state subtree")
            for member in members:
                member_path = pathlib.PurePosixPath(member.name)
                if (
                    not member_path.is_absolute()
                    and len(member_path.parts) < len(prefix)
                    and member_path.parts == prefix[:len(member_path.parts)]
                ):
                    if not member.isdir():
                        raise PromotionError("processor state archive has a non-directory parent of the owned state root")
                    continue
                if member_path.is_absolute() or member_path.parts[:len(prefix)] != prefix:
                    raise PromotionError("processor state archive contains a path outside the owned state root")
                relative = pathlib.PurePosixPath(*member_path.parts[len(prefix):])
                if any(part in {"", ".", ".."} for part in relative.parts):
                    raise PromotionError("processor state archive contains an unsafe path")
                if not relative.parts:
                    if not member.isdir():
                        raise PromotionError("processor state archive root is not a directory")
                    continue
                if relative in seen:
                    raise PromotionError("processor state archive contains duplicate paths")
                seen.add(relative)
                output_path = destination.joinpath(*relative.parts)
                if member.isdir():
                    output_path.mkdir(parents=True, exist_ok=True)
                    output_path.chmod(0o755)
                    continue
                if not member.isfile() or member.size < 0:
                    raise PromotionError("processor state archive contains a link or unsupported file type")
                total_bytes += member.size
                if total_bytes > MAX_PROCESSOR_ARCHIVE_BYTES:
                    raise PromotionError("processor state archive exceeds the bounded export size")
                output_path.parent.mkdir(parents=True, exist_ok=True)
                source = archive.extractfile(member)
                if source is None:
                    raise PromotionError("processor state archive contains an unreadable file")
                with source, output_path.open("xb") as sink:
                    shutil.copyfileobj(source, sink, length=1024 * 1024)
                output_path.chmod(0o644)
    finally:
        archive_path.unlink(missing_ok=True)
    index_path = destination / "sources/data_go_kr/index.json"
    if not index_path.is_file() or index_path.is_symlink():
        raise PromotionError("processor state pin does not contain the owned generation index")
    provenance = {
        "repository": repository,
        "branch_ref": state_ref,
        "branch_head_sha": remote_head,
        "processor_state_sha": processor_state_sha,
    }
    return destination, provenance


def verify_processor_checkpoint(value: dict[str, Any], schema_path: pathlib.Path) -> dict[str, Any]:
    import jsonschema

    if value.get("schema_version") != PROCESSOR_SCHEMA:
        raise PromotionError("processor state uses an unsupported checkpoint schema")
    schema = load_object(schema_path)
    jsonschema.Draft202012Validator(schema, format_checker=jsonschema.FormatChecker()).validate(value)
    unsigned = dict(value)
    claimed = unsigned.pop("checkpoint_sha256", None)
    if claimed != hashlib.sha256(canonical_json(unsigned)).hexdigest():
        raise PromotionError("processor checkpoint digest is invalid")
    generation_inputs = value.get("generation_inputs")
    if not isinstance(generation_inputs, Mapping):
        raise PromotionError("processor checkpoint lacks generation input provenance")
    if (
        generation_inputs.get("source_id") != value.get("source_id")
        or generation_inputs.get("source_scope") != value.get("source_scope")
    ):
        raise PromotionError("processor checkpoint generation source identity is inconsistent")
    expected_generation_id = hashlib.sha256(canonical_json(generation_inputs)).hexdigest()
    if value.get("generation_id") != expected_generation_id:
        raise PromotionError("processor checkpoint generation id does not bind its immutable inputs")
    return value


def locate_processor_checkpoint(
    state_root: pathlib.Path,
    processor_run_id: str,
    *,
    repository: str,
    workflow_run_id: str,
    artifact_id: str,
    schema_path: pathlib.Path,
) -> tuple[pathlib.Path, dict[str, Any]]:
    source_root = state_root / "sources/data_go_kr/generations"
    matches = []
    for path in sorted(source_root.glob("*.json")):
        checkpoint = verify_processor_checkpoint(load_object(path), schema_path)
        locator = checkpoint.get("output_artifact", {})
        if locator.get("run_id") == workflow_run_id and locator.get("name") == f"{PROCESSING_PREFIX}{processor_run_id}":
            matches.append((path, checkpoint))
    if len(matches) != 1:
        raise PromotionError("processor state does not contain exactly one artifact locator for this workflow attempt")
    path, checkpoint = matches[0]
    locator = checkpoint["output_artifact"]
    if locator.get("repository", "").lower() != repository.lower():
        raise PromotionError("processor artifact locator repository does not match this workflow")
    if str(locator.get("artifact_id")) != str(artifact_id):
        raise PromotionError("processor artifact id differs from the final CAS-published checkpoint")
    if checkpoint.get("status") not in {"ready", "no-change", "retry", "quarantined"}:
        raise PromotionError(f"processor state is not reviewable: {checkpoint.get('status')}")
    if not locator.get("bundle_manifest_sha256"):
        raise PromotionError("processor checkpoint has no output bundle manifest digest")
    return path, checkpoint


def parse_utc_timestamp(value: Any, label: str) -> dt.datetime:
    if not isinstance(value, str) or not value:
        raise PromotionError(f"{label} timestamp is missing")
    try:
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise PromotionError(f"{label} timestamp is invalid") from exc
    if parsed.tzinfo is None:
        raise PromotionError(f"{label} timestamp must include a timezone")
    return parsed.astimezone(dt.timezone.utc)


def processor_attempt_from_locator(checkpoint: Mapping[str, Any]) -> tuple[str, str, str]:
    locator = checkpoint.get("output_artifact")
    if not isinstance(locator, Mapping):
        raise PromotionError("ready processor checkpoint has no artifact locator")
    run_id = str(locator.get("run_id", ""))
    name = str(locator.get("name", ""))
    match = re.fullmatch(r"upstream-catalogue-processing-([0-9]{6,20})-([1-9][0-9]*)", name)
    if not re.fullmatch(r"[0-9]{6,20}", run_id) or not match or match.group(1) != run_id:
        raise PromotionError("ready processor artifact name does not bind one exact run attempt")
    return run_id, match.group(2), name


def validate_trusted_processor_run(
    value: Mapping[str, Any],
    *,
    repository: str,
    run_id: str,
    attempt: str,
    default_branch: str,
    expected_head_sha: str | None = None,
) -> dict[str, Any]:
    """Validate one exact completed B attempt from GitHub's run API."""
    path = str(value.get("path", "")).split("@", 1)[0]
    run_repository = value.get("repository")
    head_repository = value.get("head_repository")
    run_repository_name = run_repository.get("full_name") if isinstance(run_repository, Mapping) else None
    repository_name = head_repository.get("full_name") if isinstance(head_repository, Mapping) else None
    source_sha = str(value.get("head_sha", ""))
    if (
        str(value.get("id", "")) != run_id
        or str(value.get("run_attempt", "")) != attempt
        or value.get("name") != PROCESSOR_WORKFLOW_NAME
        or path != PROCESSOR_WORKFLOW_PATH
        or str(run_repository_name or "").casefold() != repository.casefold()
        or str(repository_name or "").lower() != repository.lower()
        or value.get("head_branch") != default_branch
        or value.get("event") not in {"schedule", "workflow_run", "workflow_dispatch"}
        or value.get("status") != "completed"
        or value.get("conclusion") != "success"
        or not re.fullmatch(r"[a-f0-9]{40}", source_sha)
        or (expected_head_sha is not None and source_sha != expected_head_sha)
    ):
        raise PromotionError("processor run is not the exact trusted successful default-branch attempt")
    return dict(value)


def validate_processor_artifact_metadata(
    artifact: Mapping[str, Any],
    checkpoint: Mapping[str, Any],
    run: Mapping[str, Any],
    *,
    repository: str,
    now: dt.datetime | None = None,
) -> dict[str, Any]:
    locator = checkpoint.get("output_artifact")
    if not isinstance(locator, Mapping):
        raise PromotionError("processor checkpoint has no output artifact locator")
    run_id, _attempt, name = processor_attempt_from_locator(checkpoint)
    actual_run = artifact.get("workflow_run")
    actual_run_id = actual_run.get("id") if isinstance(actual_run, Mapping) else None
    run_head = str(run.get("head_sha", ""))
    if (
        str(locator.get("repository", "")).lower() != repository.lower()
        or str(locator.get("run_id", "")) != run_id
        or str(locator.get("artifact_id", "")) != str(artifact.get("id", ""))
        or str(locator.get("artifact_id", "")) in {"", "None"}
        or artifact.get("name") != name
        or artifact.get("expired") is not False
        or str(actual_run_id) != run_id
        or not isinstance(actual_run, Mapping)
        or actual_run.get("head_sha") != run_head
        or actual_run.get("head_branch") != run.get("head_branch")
    ):
        raise PromotionError("processor artifact metadata differs from the durable exact run locator")
    size = artifact.get("size_in_bytes")
    if isinstance(size, bool) or not isinstance(size, int) or size < 1 or size > MAX_PROCESSOR_ARCHIVE_BYTES:
        raise PromotionError("processor artifact archive size is invalid or outside the bounded download limit")
    locator_expiry = parse_utc_timestamp(locator.get("expires_at"), "durable processor artifact expiry")
    actual_expiry = parse_utc_timestamp(artifact.get("expires_at"), "processor artifact expiry")
    current = now or dt.datetime.now(dt.timezone.utc)
    if actual_expiry != locator_expiry or actual_expiry <= current:
        raise PromotionError("processor artifact is expired or its expiry differs from the durable locator")
    digest = artifact.get("digest")
    if digest is not None and (not isinstance(digest, str) or not re.fullmatch(r"sha256:[a-f0-9]{64}", digest)):
        raise PromotionError("processor artifact archive digest metadata is malformed")
    return dict(artifact)


def list_recoverable_processor_checkpoints(
    state_root: pathlib.Path,
    schema_path: pathlib.Path,
    journal: Mapping[str, Any] | None,
    *,
    now: dt.datetime | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
    """List ordered ready checkpoints and report unusable generations without starving later work."""
    source_root = state_root / "sources/data_go_kr"
    index_path = source_root / "index.json"
    generation_root = source_root / "generations"
    if not index_path.is_file():
        return [], []
    index = load_object(index_path)
    rows = index.get("generations")
    if index.get("schema_version") != PROCESSOR_SCHEMA or not isinstance(rows, list):
        raise PromotionError("durable processor generation index is malformed or unsupported")
    current = now or dt.datetime.now(dt.timezone.utc)
    candidates: list[dict[str, Any]] = []
    blocked: list[dict[str, str]] = []
    seen: set[str] = set()
    for row in rows:
        if not isinstance(row, Mapping):
            raise PromotionError("durable processor generation index contains a malformed row")
        generation_id = row.get("generation_id")
        if not isinstance(generation_id, str) or not re.fullmatch(r"[a-f0-9]{64}", generation_id):
            raise PromotionError("durable processor generation index contains an invalid generation id")
        if generation_id in seen:
            raise PromotionError("durable processor generation index contains a duplicate generation")
        seen.add(generation_id)
        if row.get("status") != "ready":
            continue
        checkpoint_name = row.get("checkpoint")
        if checkpoint_name != f"{generation_id}.json":
            blocked.append({"generation_id": generation_id, "reason": "ready_checkpoint_path_mismatch"})
            continue
        path = generation_root / checkpoint_name
        if not path.is_file() or path.is_symlink():
            blocked.append({"generation_id": generation_id, "reason": "ready_checkpoint_missing"})
            continue
        try:
            checkpoint = verify_processor_checkpoint(load_object(path), schema_path)
        except Exception:
            blocked.append({"generation_id": generation_id, "reason": "ready_checkpoint_invalid"})
            continue
        if checkpoint.get("generation_id") != generation_id or path.stem != generation_id:
            blocked.append({"generation_id": generation_id, "reason": "checkpoint_generation_mismatch"})
            continue
        if checkpoint.get("status") != "ready":
            blocked.append({"generation_id": generation_id, "reason": "checkpoint_status_mismatch"})
            continue
        if checkpoint.get("source_id") != "data_go_kr" or checkpoint.get("source_scope") != "aggregate_supported_catalog":
            continue
        locator = checkpoint.get("output_artifact")
        if not isinstance(locator, Mapping) or not locator.get("artifact_id") or not locator.get("bundle_manifest_sha256"):
            blocked.append({"generation_id": generation_id, "reason": "ready_artifact_locator_incomplete"})
            continue
        try:
            expiry = parse_utc_timestamp(locator.get("expires_at"), "durable processor artifact expiry")
        except PromotionError:
            blocked.append({"generation_id": generation_id, "reason": "ready_artifact_expiry_invalid"})
            continue
        if expiry <= current:
            blocked.append({"generation_id": generation_id, "reason": "ready_artifact_expired"})
            continue
        try:
            processor_attempt_from_locator(checkpoint)
        except PromotionError:
            blocked.append({"generation_id": generation_id, "reason": "ready_artifact_attempt_invalid"})
            continue
        try:
            parse_utc_timestamp(checkpoint.get("observed_at"), "processor observation")
        except PromotionError:
            blocked.append({"generation_id": generation_id, "reason": "processor_observation_time_invalid"})
            continue
        candidates.append(checkpoint)
    if not candidates:
        return [], blocked
    candidates.sort(key=lambda row: (
        parse_utc_timestamp(row.get("observed_at"), "processor observation"),
        str(row.get("generation_id", "")),
    ))
    return candidates, blocked


def select_recoverable_processor_checkpoint(
    state_root: pathlib.Path,
    schema_path: pathlib.Path,
    journal: Mapping[str, Any] | None,
    *,
    now: dt.datetime | None = None,
) -> dict[str, Any] | None:
    """Return the oldest locally viable checkpoint (API checks happen before selection)."""
    candidates, _blocked = list_recoverable_processor_checkpoints(state_root, schema_path, journal, now=now)
    return candidates[0] if candidates else None


def verify_processor_input_compatibility(
    root: pathlib.Path,
    checkpoint: Mapping[str, Any],
    processor_head_sha: str,
    current_head_sha: str,
    composition_receipt: Mapping[str, Any] | None = None,
) -> None:
    """Permit an older B run only when its relevant source contract is byte-identical."""
    if not re.fullmatch(r"[a-f0-9]{40}", processor_head_sha) or not re.fullmatch(r"[a-f0-9]{40}", current_head_sha):
        raise PromotionError("processor/current source commits must be full immutable Git SHAs")
    ancestry = command(("git", "merge-base", "--is-ancestor", processor_head_sha, current_head_sha), root, allowed_returncodes=frozenset({0, 1}))
    if ancestry.returncode != 0:
        raise PromotionError("processor source commit is not an ancestor of the current trusted default branch")
    generation_inputs = checkpoint.get("generation_inputs")
    if not isinstance(generation_inputs, Mapping):
        raise PromotionError("processor checkpoint lacks generation input provenance")
    historical_bytes: dict[str, bytes] = {}
    compatibility_files = list(PROCESSOR_COMPATIBILITY_FILES)
    derivation_value = generation_inputs.get("same_observation_derivation")
    if derivation_value is not None:
        compatibility_files.append("scripts/upstream_catalogue_derivation.py")
    for raw_path in dict.fromkeys(compatibility_files):
        path = pathlib.PurePosixPath(raw_path)
        if path.is_absolute() or ".." in path.parts or "\\" in raw_path:
            raise PromotionError("processor compatibility contract contains an unsafe source path")
        try:
            source_bytes = command(("git", "show", f"{processor_head_sha}:{raw_path}"), root).stdout.encode("utf-8")
            current_bytes = (root / raw_path).read_bytes()
        except (OSError, PromotionError) as exc:
            raise PromotionError(f"current main or processor source is missing a compatibility input: {raw_path}") from exc
        source_digest = hashlib.sha256(source_bytes).hexdigest()
        historical_bytes[raw_path] = source_bytes
        current_digest = hashlib.sha256(current_bytes).hexdigest()
        expected_field = next((field for field, input_path in PROCESSOR_INPUT_PROVENANCE.items() if input_path == raw_path), None)
        if expected_field is not None and expected_field != "generator_revision" and generation_inputs.get(expected_field) != source_digest:
            raise PromotionError(f"processor checkpoint provenance does not match its source commit: {raw_path}")
        if source_digest != current_digest:
            raise PromotionError(f"processor input contract changed since observation: {raw_path}")
    processor_path = PROCESSOR_INPUT_PROVENANCE["generator_revision"]
    handoff_path = "scripts/upstream_catalogue_handoff.py"
    declaration_helper_path = "scripts/seoul_oa109_operation_declaration.py"
    snapshot_generator_path = "scripts/generate-seoul-oa109-subject-snapshot.py"
    declaration_path = "contracts/provider-operation-declarations/data-go-kr-15056854-oa-109-search-last-train-time.v1.json"
    historical_snapshot_path = "contracts/provider-operation-declarations/data-go-kr-15056854-historical-subject-0085.v1.json"
    processor_bytes = historical_bytes.get(processor_path)
    handoff_bytes = historical_bytes.get(handoff_path)
    declaration_helper_bytes = historical_bytes.get(declaration_helper_path)
    snapshot_generator_bytes = historical_bytes.get(snapshot_generator_path)
    declaration_bytes = historical_bytes.get(declaration_path)
    historical_snapshot_bytes = historical_bytes.get(historical_snapshot_path)
    if any(value is None for value in (
        processor_bytes, handoff_bytes, declaration_helper_bytes, snapshot_generator_bytes,
        declaration_bytes, historical_snapshot_bytes,
    )):
        raise PromotionError("processor source is missing a collector handoff or operation declaration compatibility input")
    generator_material = {
        "processor_script_sha256": hashlib.sha256(processor_bytes).hexdigest(),
        "collector_handoff_helper_sha256": hashlib.sha256(handoff_bytes).hexdigest(),
        "seoul_operation_declaration_helper_sha256": hashlib.sha256(declaration_helper_bytes).hexdigest(),
        "seoul_historical_subject_snapshot_generator_sha256": hashlib.sha256(snapshot_generator_bytes).hexdigest(),
        "seoul_operation_declaration_sha256": hashlib.sha256(declaration_bytes).hexdigest(),
        "seoul_historical_subject_snapshot_sha256": hashlib.sha256(historical_snapshot_bytes).hexdigest(),
    }
    if derivation_value is not None:
        derivation_bytes = historical_bytes.get("scripts/upstream_catalogue_derivation.py")
        if derivation_bytes is None:
            raise PromotionError("derived processor source is missing its same-observation lineage validator")
        derivation_material = {
            "processor_script_sha256": hashlib.sha256(processor_bytes).hexdigest(),
            "collector_handoff_helper_sha256": hashlib.sha256(handoff_bytes).hexdigest(),
            "same_observation_derivation_helper_sha256": hashlib.sha256(derivation_bytes).hexdigest(),
        }
        derivation_revision = hashlib.sha256(
            json.dumps(derivation_material, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        if derivation_value.get("derivation_processor_revision_sha256") != derivation_revision:
            raise PromotionError("processor same-observation revision does not bind its trusted source commit")
    expected_generator_revision = hashlib.sha256(
        json.dumps(generator_material, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    if generation_inputs.get("generator_revision") != expected_generator_revision:
        raise PromotionError("processor checkpoint generator revision does not bind its handoff and declaration inputs")
    if composition_receipt is not None:
        input_digests = composition_receipt.get("input_digests")
        if not isinstance(input_digests, Mapping):
            raise PromotionError("composition receipt lacks exact producer input digests")
        for input_name, raw_path in PROCESSOR_COMPOSITION_INPUTS.items():
            observed = input_digests.get(input_name)
            expected_bytes = historical_bytes.get(raw_path)
            if not isinstance(observed, Mapping) or expected_bytes is None:
                raise PromotionError(f"composition receipt is missing its exact {input_name} input digest")
            if (
                observed.get("bytes") != len(expected_bytes)
                or observed.get("sha256") != hashlib.sha256(expected_bytes).hexdigest()
            ):
                raise PromotionError(f"composition receipt {input_name} digest differs from the trusted producer commit")


def gh_rest_json(root: pathlib.Path, endpoint: str, *arguments: str) -> Any:
    argv = (
        "gh", "api", "--header", f"X-GitHub-Api-Version: {GITHUB_API_VERSION}",
        "--header", "Accept: application/vnd.github+json", *arguments, endpoint,
    )
    print(f"+ [{root}] {shlex.join(argv)}", flush=True)
    result = subprocess.run(argv, cwd=root, text=True, capture_output=True, check=False)
    if result.returncode != 0:
        status = re.search(r"\bHTTP\s+(404|410)\b", result.stderr, flags=re.IGNORECASE)
        if status and ("/actions/runs/" in endpoint or "/actions/artifacts" in endpoint):
            raise ProcessorCandidateError("the exact processor Actions run or artifact is no longer available")
        raise PromotionError(f"GitHub REST read failed ({result.returncode})")
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise PromotionError("GitHub REST endpoint returned invalid JSON") from exc


def gh_rest_bytes(root: pathlib.Path, endpoint: str) -> bytes:
    argv = ("gh", "api", "--header", f"X-GitHub-Api-Version: {GITHUB_API_VERSION}",
            "--header", "Accept: application/vnd.github+json", endpoint)
    print(f"+ [{root}] {shlex.join(argv)}", flush=True)
    result = subprocess.run(argv, cwd=root, capture_output=True, check=False)
    if result.returncode != 0:
        status = re.search(rb"\bHTTP\s+(404|410)\b", result.stderr, flags=re.IGNORECASE)
        if status:
            raise ProcessorArtifactUnavailable("the exact processor artifact is no longer available")
        raise PromotionError(f"GitHub artifact API download failed ({result.returncode})")
    return result.stdout


def github_api_request(
    method: str,
    endpoint: str,
    body: Mapping[str, Any] | None = None,
) -> tuple[int, Any]:
    token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
    if not token:
        raise PromotionError("GitHub token is unavailable for the exact-head CI operation")
    base_url = os.environ.get("GITHUB_API_URL", "https://api.github.com").rstrip("/")
    url = f"{base_url}/{endpoint.lstrip('/')}"
    payload = json.dumps(body, separators=(",", ":")).encode("utf-8") if body is not None else None
    request = urllib.request.Request(
        url,
        data=payload,
        method=method,
        headers={
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
            "X-GitHub-Api-Version": GITHUB_API_VERSION,
            **({"Content-Type": "application/json"} if payload is not None else {}),
        },
    )
    print(f"+ GitHub API {method} {endpoint}", flush=True)
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            status = int(response.status)
            raw = response.read()
    except urllib.error.HTTPError as exc:
        status = int(exc.code)
        raw = exc.read()
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise PromotionError(f"GitHub API {method} transport failed for {endpoint}") from exc
    if not raw:
        return status, None
    try:
        return status, json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return status, None


def github_api_get(endpoint: str) -> dict[str, Any]:
    status, value = github_api_request("GET", endpoint)
    if status != 200 or not isinstance(value, dict):
        raise PromotionError(f"GitHub API read failed with HTTP {status}")
    return value


def verify_release_branch_sha(repository: str, branch: str) -> str | None:
    ref = urllib.parse.quote(branch, safe="/")
    status, value = github_api_request("GET", f"repos/{repository}/git/ref/heads/{ref}")
    if status == 404:
        return None
    if status != 200 or not isinstance(value, Mapping):
        raise PromotionError(f"candidate branch API read failed with HTTP {status}")
    obj = value.get("object")
    sha = obj.get("sha") if isinstance(obj, Mapping) else None
    return str(sha) if isinstance(sha, str) else None


def list_verify_release_runs(
    repository: str,
    workflow_path: str,
    branch: str,
    event: str,
    head_sha: str,
) -> list[dict[str, Any]]:
    workflow = urllib.parse.quote(workflow_path, safe="")
    query = urllib.parse.urlencode({
        "event": event, "head_sha": head_sha, "branch": branch, "per_page": 100,
    })
    value = github_api_get(f"repos/{repository}/actions/workflows/{workflow}/runs?{query}")
    rows = value.get("workflow_runs")
    total = value.get("total_count")
    if (
        not isinstance(rows, list)
        or isinstance(total, bool)
        or not isinstance(total, int)
        or total < 0
        or total > 100
        or len(rows) != total
        or any(not isinstance(row, dict) for row in rows)
    ):
        raise PromotionError("matching verify-release run list is malformed or exceeds its bounded result limit")
    return rows


def read_verify_release_run(repository: str, run_id: int) -> dict[str, Any]:
    for _attempt_read in range(2):
        run = github_api_get(f"repos/{repository}/actions/runs/{run_id}")
        current_attempt = run.get("run_attempt")
        if isinstance(current_attempt, bool) or not isinstance(current_attempt, int) or current_attempt < 1:
            raise PromotionError("verify-release run API omitted its exact current attempt")
        jobs_query = urllib.parse.urlencode({"per_page": 100})
        jobs = github_api_get(
            f"repos/{repository}/actions/runs/{run_id}/attempts/{current_attempt}/jobs?{jobs_query}"
        )
        job_rows = jobs.get("jobs")
        job_total = jobs.get("total_count")
        if (
            not isinstance(job_rows, list)
            or isinstance(job_total, bool)
            or not isinstance(job_total, int)
            or job_total < 0
            or job_total > 100
            or len(job_rows) != job_total
            or any(not isinstance(row, dict) for row in job_rows)
        ):
            raise PromotionError("verify-release attempt jobs are malformed or exceed the bounded result limit")
        after_jobs = github_api_get(f"repos/{repository}/actions/runs/{run_id}")
        if after_jobs.get("run_attempt") != current_attempt:
            continue
        after_jobs["jobs"] = job_rows
        return after_jobs
    raise PromotionError("verify-release run attempt changed while its required jobs were being read")


def processor_run_api(root: pathlib.Path, repository: str, run_id: str, attempt: str) -> dict[str, Any]:
    value = gh_rest_json(root, f"repos/{repository}/actions/runs/{run_id}/attempts/{attempt}")
    if not isinstance(value, dict):
        raise PromotionError("processor run attempt API returned a malformed value")
    return value


def processor_artifact_api(root: pathlib.Path, repository: str, run_id: str, artifact_id: str) -> dict[str, Any] | None:
    matches: list[dict[str, Any]] = []
    total_count: int | None = None
    for page in range(1, MAX_PROCESSOR_ARTIFACT_PAGES + 1):
        payload = gh_rest_json(
            root, f"repos/{repository}/actions/runs/{run_id}/artifacts",
            "--method", "GET", "-F", "per_page=100", "-F", f"page={page}",
        )
        if (
            not isinstance(payload, dict)
            or not isinstance(payload.get("artifacts"), list)
            or isinstance(payload.get("total_count"), bool)
            or not isinstance(payload.get("total_count"), int)
            or payload["total_count"] < 0
        ):
            raise PromotionError("processor artifact API returned a malformed listing")
        if total_count is None:
            total_count = payload["total_count"]
        elif total_count != payload["total_count"]:
            raise PromotionError("processor artifact API listing changed while reading bounded pages")
        matches.extend(
            row for row in payload["artifacts"]
            if isinstance(row, dict) and str(row.get("id", "")) == artifact_id
        )
        if len(matches) > 1:
            raise PromotionError("processor state resolves to duplicate GitHub artifact ids")
        if page * 100 >= total_count:
            return matches[0] if matches else None
    if total_count is not None and total_count > MAX_PROCESSOR_ARTIFACT_PAGES * 100:
        raise PromotionError("processor artifact API listing exceeded its bounded pagination limit")
    return matches[0] if matches else None


def download_processor_artifact(
    root: pathlib.Path,
    repository: str,
    metadata: Mapping[str, Any],
    output_dir: pathlib.Path,
) -> pathlib.Path:
    artifact_id = str(metadata.get("id", ""))
    raw = gh_rest_bytes(root, f"repos/{repository}/actions/artifacts/{artifact_id}/zip")
    size = metadata.get("size_in_bytes")
    if len(raw) != size:
        raise ProcessorCandidateError("downloaded processor artifact byte count differs from GitHub metadata")
    digest = metadata.get("digest")
    if isinstance(digest, str) and hashlib.sha256(raw).hexdigest() != digest.removeprefix("sha256:"):
        raise ProcessorCandidateError("downloaded processor archive digest differs from GitHub metadata")
    output_dir = output_dir.resolve()
    if output_dir.exists():
        shutil.rmtree(output_dir)
    output_dir.mkdir(parents=True)
    archive = output_dir.parent / f"{output_dir.name}.zip"
    archive.write_bytes(raw)
    expected = set(REQUIRED_PROCESSOR_FILES) | {"upstream-catalogue-checkpoint-receipt.json"}
    total = 0
    try:
        with zipfile.ZipFile(archive) as bundle:
            members = bundle.infolist()
            names = [member.filename for member in members]
            if len(names) != len(set(names)) or set(names) != expected:
                raise ProcessorCandidateError("processor artifact does not contain the exact frozen bundle file set")
            for member in members:
                name = member.filename
                path = pathlib.PurePosixPath(name)
                mode = member.external_attr >> 16
                if (
                    path.is_absolute() or ".." in path.parts or "\\" in name
                    or len(path.parts) != 1 or member.is_dir() or stat.S_ISLNK(mode)
                ):
                    raise ProcessorCandidateError("processor artifact contains an unsafe path, directory, or link")
                maximum = 64 * 1024 * 1024 if name == "upstream-catalogue-checkpoint-receipt.json" else 512 * 1024 * 1024
                if member.file_size < 0 or member.file_size > maximum:
                    raise ProcessorCandidateError("processor artifact member exceeds its bounded byte limit")
                total += member.file_size
                if total > MAX_PROCESSOR_BUNDLE_BYTES:
                    raise ProcessorCandidateError("processor artifact expands beyond its bounded bundle limit")
                target = output_dir / name
                target.write_bytes(bundle.read(member))
    except (OSError, zipfile.BadZipFile, RuntimeError) as exc:
        raise ProcessorCandidateError("processor artifact is not a readable immutable ZIP bundle") from exc
    return output_dir


def validate_processor_link_metadata(
    checkpoint: Mapping[str, Any],
    bundle_dir: pathlib.Path,
    *,
    root: pathlib.Path,
    composition_baseline_sha256: str | None = None,
    composition_baseline_bytes: int | None = None,
    canonical_context: Mapping[str, Any] | None = None,
    allow_terminal_noop: bool = False,
    validate_seoul: bool = True,
) -> list[dict[str, Any]]:
    """Semantically bind unresolved resolver metadata to its checkpoint row."""
    evidence_path = bundle_dir / "upstream-catalogue-enrichment-evidence.json"
    if not evidence_path.is_file():
        detail_records = checkpoint.get("detail_records")
        if isinstance(detail_records, list) and any(
            isinstance(item, Mapping)
            and (
                "link_metadata" in item
                or isinstance(item.get("failure_diagnostic"), Mapping)
                and item["failure_diagnostic"].get("code") == "resolved_link_operation_contract_unproven"
            )
            for item in detail_records
        ):
            raise PromotionError("processor resolver checkpoint metadata is missing its enrichment evidence")
        return []
    evidence = load_object(evidence_path)
    if not isinstance(evidence, Mapping):
        raise PromotionError("processor enrichment evidence is not an object")
    try:
        import jsonschema
    except ImportError as exc:
        raise PromotionError("processor enrichment schema validator is unavailable") from exc
    try:
        enrichment_schema = load_object(root / "schemas/datapan.catalogue-enrichment-evidence.v1.schema.json")
        jsonschema.Draft202012Validator(
            enrichment_schema, format_checker=jsonschema.FormatChecker(),
        ).validate(evidence)
    except (OSError, PromotionError, jsonschema.ValidationError) as exc:
        raise PromotionError("processor enrichment evidence does not match the trusted schema") from exc
    if validate_seoul:
        validate_processor_seoul_declaration(
            checkpoint, bundle_dir, evidence, root=root,
            composition_baseline_sha256=composition_baseline_sha256,
            composition_baseline_bytes=composition_baseline_bytes,
            canonical_context=canonical_context,
            allow_terminal_noop=allow_terminal_noop,
        )
    outcomes = evidence.get("worker_outcomes", [])
    if not isinstance(outcomes, list):
        raise PromotionError("processor worker outcomes are not an array")
    metadata_outcomes = [
        item for item in outcomes if isinstance(item, Mapping) and "link_metadata" in item
    ]
    detail_records = checkpoint.get("detail_records")
    if not isinstance(detail_records, list):
        raise PromotionError("processor checkpoint detail records are invalid")
    metadata_records = [
        item for item in detail_records if isinstance(item, Mapping) and "link_metadata" in item
    ]
    for item in outcomes:
        diagnostic = item.get("failure_diagnostic") if isinstance(item, Mapping) else None
        if (
            isinstance(diagnostic, Mapping)
            and diagnostic.get("code") == "resolved_link_operation_contract_unproven"
            and "link_metadata" not in item
        ):
            raise PromotionError("processor resolver diagnostic is missing its link metadata provenance")
    for item in detail_records:
        diagnostic = item.get("failure_diagnostic") if isinstance(item, Mapping) else None
        if (
            isinstance(diagnostic, Mapping)
            and diagnostic.get("code") == "resolved_link_operation_contract_unproven"
            and "link_metadata" not in item
        ):
            raise PromotionError("processor checkpoint resolver diagnostic is missing its link metadata provenance")
    if not metadata_outcomes and not metadata_records:
        return []
    try:
        composer = load_module(
            root / "scripts/compose-upstream-catalogue-candidate.py",
            "processor_catalogue_composer_for_bundle_validation",
        )
    except (OSError, PromotionError) as exc:
        raise PromotionError("trusted processor composer is unavailable for link metadata") from exc
    helper = getattr(composer, "LINK_DETAIL_HELPERS", None)
    if helper is None or not callable(getattr(helper, "validate_link_metadata", None)):
        raise PromotionError("processor link metadata validator is unavailable")
    try:
        provider_index = load_object(root / "data/provider-index.json")
        registered = helper.registered_hosts(provider_index)
    except (OSError, PromotionError, AttributeError, TypeError) as exc:
        raise PromotionError("trusted provider host index is unavailable for link metadata") from exc
    by_id: dict[str, Mapping[str, Any]] = {}
    for item in detail_records:
        if not isinstance(item, Mapping):
            continue
        identity = str(item.get("id") or "")
        if identity:
            if identity in by_id:
                raise PromotionError("processor checkpoint has duplicate detail identities")
            by_id[identity] = item
    seen: set[str] = set()
    contract_diagnostics: list[dict[str, Any]] = []
    successful = {
        str(item.get("api_key", {}).get("id") or "")
        for item in evidence.get("records", [])
        if isinstance(item, Mapping) and isinstance(item.get("api_key"), Mapping)
    } if isinstance(evidence.get("records", []), list) else set()
    for item in metadata_outcomes:
        api_key = item.get("api_key")
        identity = str(api_key.get("id") or "") if isinstance(api_key, Mapping) else ""
        diagnostic = item.get("failure_diagnostic")
        row = by_id.get(identity)
        if (
            not identity or not isinstance(api_key, Mapping) or api_key.get("provider") != "data.go.kr"
            or identity in seen or identity in successful
            or item.get("status") != "quarantined"
            or not isinstance(diagnostic, Mapping)
            or diagnostic.get("code") != "resolved_link_operation_contract_unproven"
            or diagnostic.get("phase") != "resolver"
            or not isinstance(row, Mapping)
            or row.get("status") != "quarantined"
            or row.get("source_sha256") != item.get("source_sha256")
            or row.get("guide_sha256") != item.get("guide_sha256")
            or row.get("failure_diagnostic") != diagnostic
            or row.get("link_metadata") != item.get("link_metadata")
        ):
            raise PromotionError("processor link metadata outcome is not bound to its quarantined checkpoint row")
        try:
            helper.validate_link_metadata(item["link_metadata"], identity, registered)
        except (ValueError, TypeError) as exc:
            raise PromotionError("processor link metadata provenance is invalid") from exc
        try:
            projection = composer.worker_contract_projection(dict(item))
        except (AttributeError, TypeError, ValueError) as exc:
            raise PromotionError("processor resolver contract diagnostic is invalid") from exc
        if not isinstance(projection, Mapping) or not projection:
            raise PromotionError("processor resolver outcome has no trusted contract diagnostic projection")
        projected = {
            "api_key": {"provider": "data.go.kr", "id": identity},
            "worker_status": item.get("status"),
            "source_sha256": item.get("source_sha256"),
            "guide_sha256": item.get("guide_sha256"),
            "worker_outcome_sha256": projection.get("worker_outcome_sha256"),
            "detail_status": (
                "verified" if "contract_failure" in projection else "legacy_detail_unknown"
            ),
            "next_action": projection.get("next_action"),
        }
        if "contract_failure" in projection:
            projected["contract_failure"] = copy.deepcopy(projection["contract_failure"])
        elif projection.get("contract_failure_status") != "legacy_detail_unknown":
            raise PromotionError("processor legacy resolver diagnostic projection is invalid")
        contract_diagnostics.append(projected)
        seen.add(identity)
    if {str(row.get("id") or "") for row in metadata_records if isinstance(row, Mapping)} != seen:
        raise PromotionError("processor checkpoint and enrichment link metadata identities differ")
    return sorted(
        contract_diagnostics,
        key=lambda row: (str(row["api_key"]["provider"]), str(row["api_key"]["id"])),
    )


CONTRACT_DIAGNOSTIC_PROJECTION_FIELDS = frozenset({
    "worker_outcome_sha256", "contract_failure", "contract_failure_status", "next_action",
})


def _terminate_and_reap_processor_composer_read(process: subprocess.Popen[bytes]) -> None:
    """Stop a bounded local identity read and deterministically reap it."""
    if process.poll() is not None:
        process.wait()
        return
    try:
        process.terminate()
    except OSError:
        pass
    try:
        process.wait(timeout=PROCESSOR_COMPOSER_TERMINATE_GRACE_SECONDS)
        return
    except subprocess.TimeoutExpired:
        pass
    try:
        process.kill()
    except OSError:
        pass
    process.wait()


def processor_composer_source_identity(
    root: pathlib.Path,
    producer_head_sha: str,
) -> dict[str, Any] | None:
    """Hash one bounded local composer blob from an authenticated B source commit."""
    if not re.fullmatch(r"[a-f0-9]{40}", producer_head_sha):
        return None
    composer_path = PROCESSOR_COMPOSITION_INPUTS["composer"]
    argv = ("git", "--no-pager", "show", f"{producer_head_sha}:{composer_path}")
    print(f"+ [{root}] {shlex.join(argv)}", flush=True)
    environment = dict(os.environ)
    environment.update({
        "GIT_NO_LAZY_FETCH": "1",
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_PAGER": "cat",
    })
    deadline = time.monotonic() + PROCESSOR_COMPOSER_READ_TIMEOUT_SECONDS
    try:
        process = subprocess.Popen(
            argv,
            cwd=root,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            close_fds=True,
            env=environment,
        )
    except OSError:
        # Missing local producer history can never grant the historical
        # encoding. The complete current projection remains mandatory.
        return None

    assert process.stdout is not None
    assert process.stderr is not None
    selector = selectors.DefaultSelector()
    digest = hashlib.sha256()
    counts = {"stdout": 0, "stderr": 0}
    streams = {
        process.stdout: ("stdout", MAX_PROCESSOR_COMPOSER_BYTES),
        process.stderr: ("stderr", MAX_PROCESSOR_COMPOSER_STDERR_BYTES),
    }
    try:
        for stream in streams:
            os.set_blocking(stream.fileno(), False)
            selector.register(stream, selectors.EVENT_READ)
        while selector.get_map():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return None
            for key, _events in selector.select(min(remaining, 0.25)):
                stream = key.fileobj
                label, maximum_bytes = streams[stream]
                read_size = min(65536, maximum_bytes + 1 - counts[label])
                if read_size < 1:
                    return None
                try:
                    chunk = os.read(stream.fileno(), read_size)
                except BlockingIOError:
                    continue
                if not chunk:
                    selector.unregister(stream)
                    continue
                counts[label] += len(chunk)
                if counts[label] > maximum_bytes:
                    return None
                if label == "stdout":
                    digest.update(chunk)
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return None
        try:
            returncode = process.wait(timeout=remaining)
        except subprocess.TimeoutExpired:
            return None
        if returncode != 0:
            return None
        return {"bytes": counts["stdout"], "sha256": digest.hexdigest()}
    except (OSError, ValueError):
        return None
    finally:
        if process.poll() is None:
            _terminate_and_reap_processor_composer_read(process)
        else:
            process.wait()
        selector.close()
        process.stdout.close()
        process.stderr.close()


def authenticate_contract_diagnostic_encoding(
    composition_receipt: Mapping[str, Any],
    producer_composer: Mapping[str, Any] | None,
) -> str:
    """Bind the historical/current row encoding to the native composer source."""
    if producer_composer is None:
        return "projected"
    input_digests = composition_receipt.get("input_digests")
    receipt_composer = input_digests.get("composer") if isinstance(input_digests, Mapping) else None
    if (
        not isinstance(receipt_composer, Mapping)
        or isinstance(receipt_composer.get("bytes"), bool)
        or not isinstance(receipt_composer.get("bytes"), int)
        or receipt_composer.get("bytes") < 1
        or not isinstance(receipt_composer.get("sha256"), str)
        or not re.fullmatch(r"[a-f0-9]{64}", receipt_composer["sha256"])
        or dict(receipt_composer) != dict(producer_composer)
    ):
        raise PromotionError("composition receipt composer identity differs from its authenticated producer source")
    return "historical" if dict(receipt_composer) == HISTORICAL_LINK_CONTRACT_COMPOSER else "projected"


def validate_processor_contract_diagnostic_outputs(
    bundle_dir: pathlib.Path,
    contract_diagnostics: Sequence[Mapping[str, Any]],
    *,
    encoding: str = "projected",
) -> None:
    """Authenticate the composer's complete per-subject diagnostic projection."""
    if encoding not in {"historical", "projected"}:
        raise PromotionError("processor contract diagnostic output encoding is unsupported")
    if len(contract_diagnostics) > 128:
        raise PromotionError("processor contract diagnostic projection exceeds its bounded subject limit")

    def identity(row: Mapping[str, Any], label: str) -> tuple[str, str]:
        api_key = row.get("api_key")
        provider = api_key.get("provider") if isinstance(api_key, Mapping) else None
        api_id = api_key.get("id") if isinstance(api_key, Mapping) else None
        if provider != "data.go.kr" or not isinstance(api_id, str) or not api_id:
            raise PromotionError(f"processor {label} has an invalid contract diagnostic identity")
        return provider, api_id

    expected: dict[tuple[str, str], Mapping[str, Any]] = {}
    for row in contract_diagnostics:
        if not isinstance(row, Mapping):
            raise PromotionError("processor contract diagnostic projection contains a non-object row")
        key = identity(row, "admitted outcome")
        if key in expected:
            raise PromotionError("processor contract diagnostic projection contains duplicate identities")
        expected[key] = row

    try:
        semantic_diff = load_object(bundle_dir / "semantic-diff.json")
        regeneration_queue = load_object(bundle_dir / "regeneration-queue.json")
        quarantine = load_object(bundle_dir / "quarantine.json")
    except PromotionError as exc:
        raise PromotionError("processor contract diagnostic composition outputs are unavailable") from exc
    decisions = semantic_diff.get("api_decisions") if isinstance(semantic_diff, Mapping) else None
    queue_rows = regeneration_queue.get("items") if isinstance(regeneration_queue, Mapping) else None
    quarantine_rows = quarantine.get("items") if isinstance(quarantine, Mapping) else None
    if not isinstance(decisions, list) or not isinstance(queue_rows, list) or not isinstance(quarantine_rows, list):
        raise PromotionError("processor contract diagnostic composition outputs are malformed")

    def indexed(rows: Sequence[Any], label: str) -> dict[tuple[str, str], Mapping[str, Any]]:
        result: dict[tuple[str, str], Mapping[str, Any]] = {}
        for row in rows:
            if not isinstance(row, Mapping):
                raise PromotionError(f"processor {label} contains a non-object row")
            key = identity(row, label)
            if key in result:
                raise PromotionError(f"processor {label} contains duplicate identities")
            result[key] = row
        return result

    decision_by_key = indexed(decisions, "semantic decision")
    queue_by_key = indexed(queue_rows, "regeneration queue")
    quarantine_by_key = indexed(quarantine_rows, "quarantine")

    def supplied_projection(row: Mapping[str, Any]) -> dict[str, Any]:
        return {name: row[name] for name in CONTRACT_DIAGNOSTIC_PROJECTION_FIELDS if name in row}

    for label, rows in (
        ("semantic decision", decision_by_key),
        ("regeneration queue", queue_by_key),
        ("quarantine", quarantine_by_key),
    ):
        unrelated = [key for key, row in rows.items() if supplied_projection(row) and key not in expected]
        if unrelated:
            raise PromotionError(f"processor {label} contains an unrelated contract diagnostic projection")

    for key, expected_row in expected.items():
        modern = expected_row.get("detail_status") == "verified"
        if encoding == "historical" and modern:
            raise PromotionError("historical composer cannot carry a modern contract diagnostic")
        shared: dict[str, Any] = {
            "worker_outcome_sha256": expected_row.get("worker_outcome_sha256"),
            "next_action": expected_row.get("next_action"),
        }
        if modern:
            contract_failure = expected_row.get("contract_failure")
            if not isinstance(contract_failure, Mapping):
                raise PromotionError("processor admitted contract diagnostic has no validated detail")
            shared["contract_failure"] = contract_failure
            required_evidence = contract_failure.get("unresolved_requirements")
        elif expected_row.get("detail_status") == "legacy_detail_unknown":
            shared["contract_failure_status"] = "legacy_detail_unknown"
            required_evidence = []
        else:
            raise PromotionError("processor admitted contract diagnostic has an invalid detail status")
        if not isinstance(required_evidence, list):
            raise PromotionError("processor admitted contract diagnostic has invalid required evidence")

        decision = decision_by_key.get(key)
        queue_row = queue_by_key.get(key)
        quarantine_row = quarantine_by_key.get(key)
        if encoding == "historical":
            shared = {}
            required_evidence = HISTORICAL_LINK_CONTRACT_REQUIRED_EVIDENCE
        worker_status = expected_row.get("worker_status")
        expected_disposition = "retain_worker_pending" if worker_status == "retry" else "quarantine"
        expected_reason = "worker_detail_retry" if worker_status == "retry" else "worker_detail_quarantined"
        if (
            worker_status not in {"retry", "quarantined"}
            or not isinstance(decision, Mapping)
            or decision.get("disposition") != expected_disposition
            or decision.get("worker_outcome_status") != worker_status
            or decision.get("worker_source_sha256") != expected_row.get("source_sha256")
            or decision.get("worker_guide_sha256") != expected_row.get("guide_sha256")
            or supplied_projection(decision) != shared
            or not isinstance(queue_row, Mapping)
            or queue_row.get("reason_codes") != [expected_reason]
            or queue_row.get("required_evidence") != required_evidence
            or supplied_projection(queue_row) != shared
        ):
            raise PromotionError("processor contract diagnostic differs from its semantic decision or queue projection")
        if worker_status == "quarantined":
            if (
                not isinstance(quarantine_row, Mapping)
                or quarantine_row.get("reason_codes") != [expected_reason]
                or quarantine_row.get("record_state") not in {"baseline_retained", "candidate_excluded"}
                or supplied_projection(quarantine_row) != shared
            ):
                raise PromotionError("processor contract diagnostic differs from its quarantine projection")
        elif quarantine_row is not None:
            raise PromotionError("processor retry contract diagnostic was incorrectly projected as quarantined")

    expected_keys = set(expected) if encoding == "projected" else set()
    projected_decisions = {key for key, row in decision_by_key.items() if supplied_projection(row)}
    projected_queue = {key for key, row in queue_by_key.items() if supplied_projection(row)}
    projected_quarantine = {key for key, row in quarantine_by_key.items() if supplied_projection(row)}
    expected_quarantine = {
        key for key, row in expected.items() if row.get("worker_status") == "quarantined"
    } if encoding == "projected" else set()
    if projected_decisions != expected_keys or projected_queue != expected_keys or projected_quarantine != expected_quarantine:
        raise PromotionError("processor contract diagnostic composition coverage is incomplete")


def processor_seoul_unchanged_baseline_rows(
    bundle_dir: pathlib.Path,
    composed_rows: list[Any],
    *,
    composition_baseline_sha256: str,
    composition_baseline_bytes: int | None,
    canonical_context: Mapping[str, Any] | None,
    allow_terminal_noop: bool,
) -> tuple[list[Any], str]:
    """Return rows backed by the exact C composition baseline or safe no-op output."""
    if not isinstance(composition_baseline_sha256, str) or not re.fullmatch(
        r"[a-f0-9]{64}", composition_baseline_sha256,
    ):
        raise PromotionError("processor Seoul unchanged decision lacks a resolved composition-baseline digest")
    if composition_baseline_bytes is not None and (
        type(composition_baseline_bytes) is not int or composition_baseline_bytes < 1
    ):
        raise PromotionError("processor Seoul unchanged decision has an invalid composition-baseline size")
    composed_path = bundle_dir / "composed-candidate.registry.json"
    composed_sha256 = file_sha256(composed_path)
    composed_size = composed_path.stat().st_size
    if (
        composed_sha256 == composition_baseline_sha256
        and (composition_baseline_bytes is None or composed_size == composition_baseline_bytes)
    ):
        return composed_rows, "generation-baseline"
    if not isinstance(canonical_context, Mapping):
        raise PromotionError("processor Seoul unchanged decision has no caller-authenticated composition baseline")
    identity = canonical_context.get("identity")
    rows = canonical_context.get("rows")
    if not isinstance(identity, Mapping) or not isinstance(rows, list) or not rows:
        raise PromotionError("processor Seoul unchanged decision canonical context is malformed")
    current_sha = identity.get("registry_sha256")
    current_size = identity.get("registry_bytes")
    if type(current_size) is not int or current_size < 1:
        raise PromotionError("processor Seoul unchanged decision canonical size is invalid")
    if current_sha == composition_baseline_sha256:
        if composition_baseline_bytes is not None and current_size != composition_baseline_bytes:
            raise PromotionError("processor Seoul unchanged decision canonical baseline size differs")
        return rows, "authenticated-composition-baseline"
    if (
        allow_terminal_noop
        and current_sha == composed_sha256
        and current_size == composed_size
    ):
        return rows, "terminal-already-canonical-noop"
    raise PromotionError("processor Seoul unchanged decision differs from authenticated composition-baseline bytes")


def validate_processor_seoul_declaration(
    checkpoint: Mapping[str, Any],
    bundle_dir: pathlib.Path,
    evidence: Mapping[str, Any],
    *,
    root: pathlib.Path,
    composition_baseline_sha256: str | None,
    composition_baseline_bytes: int | None,
    canonical_context: Mapping[str, Any] | None,
    allow_terminal_noop: bool = False,
) -> None:
    """Revalidate a successful pinned declaration against the trusted source and C outputs."""
    try:
        composer = load_module(
            root / "scripts/compose-upstream-catalogue-candidate.py",
            "processor_catalogue_composer_for_declaration_validation",
        )
    except (OSError, PromotionError) as exc:
        raise PromotionError("trusted processor composer is unavailable for declaration validation") from exc
    declaration = getattr(composer, "SEOUL_OPERATION_DECLARATION", None)
    declaration_id = getattr(declaration, "DECLARATION_ID", None)
    if not isinstance(declaration_id, str) or not declaration_id:
        raise PromotionError("trusted Seoul operation declaration validator is unavailable")
    target_id = str(declaration.DECLARATION["subject"]["portal_dataset_id"])
    records = evidence.get("records")
    if not isinstance(records, list):
        raise PromotionError("processor enrichment records are invalid for declaration validation")

    def declared_operations(row: Any) -> list[Mapping[str, Any]]:
        if not isinstance(row, Mapping) or not isinstance(row.get("operations"), list):
            return []
        result = []
        for operation in row["operations"]:
            source = operation.get("source") if isinstance(operation, Mapping) else None
            raw = source.get("raw") if isinstance(source, Mapping) else None
            if isinstance(raw, Mapping) and raw.get("operation_declaration_id") is not None:
                result.append(operation)
        return result

    declared_records: list[Mapping[str, Any]] = []
    target_records: list[Mapping[str, Any]] = []
    for record in records:
        if not isinstance(record, Mapping):
            raise PromotionError("processor enrichment record is invalid for declaration validation")
        api_key = record.get("api_key")
        identity = str(api_key.get("id") or "") if isinstance(api_key, Mapping) else ""
        if identity == target_id:
            target_records.append(record)
        if record.get("declaration_provenance") is not None or declared_operations(record):
            if identity != target_id:
                raise PromotionError("processor declaration provenance is bound to the wrong API identity")
            declared_records.append(record)

    checkpoint_details = checkpoint.get("detail_records")
    if not isinstance(checkpoint_details, list):
        raise PromotionError("processor checkpoint detail records are invalid for declaration validation")
    target_success = [
        item for item in checkpoint_details
        if isinstance(item, Mapping) and str(item.get("id") or "") == target_id and item.get("status") == "enriched"
    ]
    if len(target_success) > 1:
        raise PromotionError("processor checkpoint has duplicate successful Seoul declaration rows")
    if target_success and (len(target_records) != 1 or len(declared_records) != 1):
        raise PromotionError("successful Seoul declaration row was downgraded or lost its declaration evidence")
    if target_records and not declared_records:
        raise PromotionError("successful Seoul enrichment record is missing its pinned declaration provenance")
    if declared_records and (len(declared_records) != 1 or target_records != declared_records):
        raise PromotionError("processor Seoul declaration evidence is duplicated or inconsistent")
    if not declared_records:
        return

    record = declared_records[0]
    if len(declared_operations(record)) != 1 or record.get("declaration_provenance") is None:
        raise PromotionError("processor Seoul declaration operation or provenance is missing")
    record_op = declared_operations(record)[0]
    try:
        # The compact source-controlled snapshot is extracted from the exact
        # authenticated 0085 payload and pinned by both declaration and helper.
        # It remains independent of current canonical bytes and works in the
        # processor's intentional pointer-only checkout.
        historical_snapshot = declaration.load_historical_subject_snapshot(root)
        pinned_source_row = copy.deepcopy(dict(historical_snapshot["subject_row"]))
        declaration.validate_subject_row(pinned_source_row)
    except (AttributeError, OSError, TypeError, ValueError) as exc:
        raise PromotionError("processor Seoul historical subject snapshot is unavailable or invalid") from exc
    composed_path = bundle_dir / "composed-candidate.registry.json"
    ready_path = bundle_dir / "ready-scope.registry.json"
    try:
        composed_rows = load_json_value(composed_path)
        ready_rows = load_json_value(ready_path)
    except PromotionError as exc:
        raise PromotionError("processor composed outputs are unavailable for declaration validation") from exc
    if not isinstance(composed_rows, list) or not isinstance(ready_rows, list):
        raise PromotionError("processor composed outputs are invalid for declaration validation")

    def find_unique_target(rows: list[Any], label: str) -> Mapping[str, Any]:
        matches = [
            row for row in rows
            if isinstance(row, Mapping) and str(row.get("id") or "") == target_id
            and row.get("provider") == "data.go.kr"
        ]
        if len(matches) != 1:
            raise PromotionError(f"processor {label} does not contain one exact Seoul declaration row")
        return matches[0]

    composed_row = find_unique_target(composed_rows, "composed candidate")
    ready_matches = [
        row for row in ready_rows
        if isinstance(row, Mapping) and str(row.get("id") or "") == target_id
        and row.get("provider") == "data.go.kr"
    ]
    if len(ready_matches) > 1:
        raise PromotionError("processor ready scope contains duplicate Seoul declaration rows")
    # Rebuild the only permitted output transform from the independently
    # authenticated committed source row. This binds each final output's full
    # subject/source/UDDI and ordered operation list; neither output is used as
    # the authority for reconstructing the other's historical prefix.
    if record.get("api_key") != {"provider": "data.go.kr", "id": target_id}:
        raise PromotionError("processor Seoul declaration record identity differs from pinned subject")
    expected_row = composer.apply_enrichment_record(copy.deepcopy(pinned_source_row), dict(record))
    if dict(composed_row) != expected_row:
        raise PromotionError(
            "processor composed candidate full Seoul subject, source, guide, or ordered operation list differs from pinned declaration"
        )
    try:
        composition_receipt = load_object(bundle_dir / "composition-receipt.json")
        semantic_diff = load_object(bundle_dir / "semantic-diff.json")
    except PromotionError as exc:
        raise PromotionError("processor Seoul declaration disposition evidence is unavailable") from exc
    scope = composition_receipt.get("scope") if isinstance(composition_receipt, Mapping) else None
    decisions = semantic_diff.get("api_decisions") if isinstance(semantic_diff, Mapping) else None
    decision_matches = [
        item for item in decisions
        if isinstance(item, Mapping)
        and isinstance(item.get("api_key"), Mapping)
        and item["api_key"].get("provider") == "data.go.kr"
        and str(item["api_key"].get("id") or "") == target_id
    ] if isinstance(decisions, list) else []
    if not isinstance(scope, Mapping) or len(decision_matches) != 1:
        raise PromotionError("processor Seoul declaration disposition is missing or ambiguous")
    decision = decision_matches[0]
    disposition = decision.get("disposition")
    status = composition_receipt.get("status")
    if ready_matches:
        ready_row = ready_matches[0]
        if dict(ready_row) != expected_row:
            raise PromotionError(
                "processor ready scope full Seoul subject, source, guide, or ordered operation list differs from pinned declaration"
            )
        if disposition not in {"accept_new", "accept_changed", "retain_enrichment"}:
            raise PromotionError("processor ready Seoul declaration lacks an applied composition decision")
    else:
        # A source-pinned declaration can be present in composed output while
        # producing no ready-scope delta only when composition independently
        # records this API as unchanged. Pending, quarantined, and changed rows
        # must remain visible in the ready scope or fail closed.
        if disposition != "unchanged":
            raise PromotionError("processor omitted a changed or pending Seoul declaration from ready scope")
        baseline_rows, baseline_proof = processor_seoul_unchanged_baseline_rows(
            bundle_dir, composed_rows,
            composition_baseline_sha256=composition_baseline_sha256,
            composition_baseline_bytes=composition_baseline_bytes,
            canonical_context=canonical_context,
            allow_terminal_noop=allow_terminal_noop,
        )
        baseline_row = find_unique_target(baseline_rows, "authenticated generation baseline")
        try:
            baseline_semantics = composer.semantic_record_view(dict(baseline_row))
            expected_semantics = composer.semantic_record_view(dict(expected_row))
            baseline_record_sha256 = composer._record_hash(dict(baseline_row))
        except (AttributeError, TypeError, ValueError) as exc:
            raise PromotionError("processor Seoul unchanged baseline row cannot be semantically validated") from exc
        if baseline_semantics != expected_semantics:
            raise PromotionError("processor Seoul unchanged decision differs from its authenticated baseline row")
        if decision.get("composed_record_sha256") != baseline_record_sha256:
            raise PromotionError("processor Seoul unchanged decision does not bind the authenticated composed row")
        if (
            baseline_proof in {"generation-baseline", "authenticated-composition-baseline"}
            and decision.get("baseline_record_sha256") != baseline_record_sha256
        ):
            raise PromotionError("processor Seoul unchanged decision does not bind the authenticated baseline row")
        for field in ("retained_pending_api_keys", "quarantined_api_keys"):
            identities = scope.get(field)
            if not isinstance(identities, list) or any(
                isinstance(item, Mapping)
                and item.get("provider") == "data.go.kr"
                and str(item.get("id") or "") == target_id
                for item in identities
            ):
                raise PromotionError("processor omitted a pending Seoul declaration from ready scope")
        if status == "no_change":
            outcome = checkpoint.get("outcome")
            if (
                checkpoint.get("status") != "no-change"
                or ready_rows
                or not isinstance(outcome, Mapping)
                or outcome.get("composer_status") != "no_change"
                or outcome.get("pending_count") != 0
                or outcome.get("detail_retry_count") != 0
            ):
                raise PromotionError("processor Seoul no-change result does not prove an empty, complete ready scope")
        elif status != "ready_scoped" or checkpoint.get("status") != "ready":
            raise PromotionError("processor omitted Seoul declaration outside a scoped unchanged decision")

    # The original A artifact itself is not part of the processor bundle. Bind
    # the successful enrichment to the exact pinned local subject without
    # claiming full A-file membership, and accept only the recorded guide
    # transform plus the one declaration operation.
    original_row = copy.deepcopy(pinned_source_row)
    try:
        declaration.validate_enriched_record(original_row, record, root=root)
        generation_inputs = checkpoint.get("generation_inputs")
        candidate_sha256 = generation_inputs.get("candidate_sha256") if isinstance(generation_inputs, Mapping) else None
        provider_index = load_object(root / "data/provider-index.json")
        provider_index_sha256 = file_sha256(root / "data/provider-index.json")
        composition = load_object(bundle_dir / "composition-receipt.json")
        input_digests = composition.get("input_digests") if isinstance(composition, Mapping) else None
        if (
            not isinstance(candidate_sha256, str)
            or not isinstance(input_digests, Mapping)
            or not isinstance(input_digests.get("candidate"), Mapping)
            or input_digests["candidate"].get("sha256") != candidate_sha256
            or not isinstance(input_digests.get("provider_index"), Mapping)
            or input_digests["provider_index"].get("sha256") != provider_index_sha256
        ):
            raise PromotionError("processor declaration evidence is not bound to trusted candidate and provider inputs")
        schema = load_object(root / "schemas/datapan.specs.v1.schema.json")
        filtered_evidence = copy.deepcopy(dict(evidence))
        filtered_evidence["records"] = [copy.deepcopy(dict(record))]
        filtered_evidence.pop("worker_outcomes", None)
        validated = composer.validate_enrichment_evidence(
            filtered_evidence,
            candidate_by_key={("data.go.kr", target_id): original_row},
            baseline_by_key={},
            candidate_sha256=candidate_sha256,
            provider_index_sha256=provider_index_sha256,
            hosts=composer.registered_hosts(provider_index),
            registry_schema=schema,
        )
        validated_record = validated.get(("data.go.kr", target_id))
        if not isinstance(validated_record, Mapping) or validated_record.get("_binding_error"):
            raise PromotionError("processor Seoul declaration semantic binding was rejected")
        declaration.validate_declared_operation(original_row, record_op, root=root)
        composer.LINK_DETAIL_HELPERS.validate_link_metadata(
            record["declaration_provenance"]["page_resolver"],
            target_id,
            composer.registered_hosts(provider_index),
        )
        if target_success:
            row = target_success[0]
            if (
                row.get("source_sha256") != record.get("source_sha256")
                or row.get("guide_sha256") != record.get("guide_sha256")
            ):
                raise PromotionError("processor Seoul declaration differs from its successful checkpoint record")
    except PromotionError:
        raise
    except Exception as exc:
        raise PromotionError("processor Seoul declaration semantic binding is invalid") from exc


def validate_processor_seoul_bundle(
    checkpoint: Mapping[str, Any],
    bundle_dir: pathlib.Path,
    bundle: Mapping[str, Any],
    *,
    root: pathlib.Path,
    canonical_context: Mapping[str, Any] | None,
    allow_terminal_noop: bool,
) -> None:
    """Apply the Seoul semantic gate after producer lineage and bundle checks."""
    enrichment_path = bundle_dir / "upstream-catalogue-enrichment-evidence.json"
    if enrichment_path.is_file() and not enrichment_path.is_symlink():
        enrichment = load_object(enrichment_path)
        validate_processor_seoul_declaration(
            checkpoint, bundle_dir, enrichment, root=root,
            composition_baseline_sha256=bundle.get("composition_baseline_sha256"),
            composition_baseline_bytes=bundle.get("composition_baseline_bytes"),
            canonical_context=canonical_context,
            allow_terminal_noop=allow_terminal_noop,
        )
        return
    target_id = "15056854"
    detail_records = checkpoint.get("detail_records")
    if isinstance(detail_records, list) and any(
        isinstance(row, Mapping)
        and str(row.get("id") or "") == target_id
        and row.get("status") == "enriched"
        for row in detail_records
    ):
        raise PromotionError("successful Seoul declaration row is missing its enrichment evidence")


def validate_processor_bundle(
    checkpoint: Mapping[str, Any],
    bundle_dir: pathlib.Path,
    composition_schema: Mapping[str, Any],
    composition_helper: Any,
    *,
    root: pathlib.Path | None = None,
    producer_head_sha: str | None = None,
    canonical_context: Mapping[str, Any] | None = None,
    allow_terminal_noop: bool = False,
    defer_seoul_declaration: bool = False,
) -> dict[str, Any]:
    digests = checkpoint.get("output_digests")
    locator = checkpoint.get("output_artifact")
    if not isinstance(digests, list) or not isinstance(locator, dict):
        raise PromotionError("processor checkpoint is missing its output bundle inventory")
    names = [row.get("path") for row in digests if isinstance(row, dict)]
    processor_status = checkpoint.get("status")
    if processor_status in {"ready", "no-change"}:
        if names != list(REQUIRED_PROCESSOR_FILES):
            raise PromotionError("reviewable processor bundle inventory does not contain the exact ordered eight-file contract")
    elif processor_status in {"retry", "quarantined"}:
        if not names or len(names) != len(set(names)) or not set(names).issubset(set(REQUIRED_PROCESSOR_FILES)):
            raise PromotionError("non-candidate bundle inventory is empty, duplicated, or contains an unrecognized output")
    else:
        raise PromotionError(f"processor state is not a terminal no-candidate or reviewable status: {processor_status}")
    if hashlib.sha256(canonical_json(digests)).hexdigest() != locator.get("bundle_manifest_sha256"):
        raise PromotionError("processor output inventory digest differs from its final checkpoint")
    if locator.get("artifact_id") is None:
        raise PromotionError("processor artifact upload has not been bound to the durable checkpoint")
    for record in digests:
        path_name = record["path"]
        relative = pathlib.PurePosixPath(path_name)
        if relative.is_absolute() or ".." in relative.parts or "\\" in path_name:
            raise PromotionError("processor bundle contains an unsafe output path")
        path = bundle_dir / path_name
        if not path.is_file() or path.is_symlink():
            raise PromotionError(f"processor bundle is missing a digest-bound output: {path_name}")
        if (path.stat().st_size, file_sha256(path)) != (record["bytes"], record["sha256"]):
            raise PromotionError(f"processor output differs from its immutable bundle manifest: {path_name}")
    checkpoint_copy = load_object(bundle_dir / "upstream-catalogue-checkpoint-receipt.json")
    if checkpoint_copy.get("generation_id") != checkpoint.get("generation_id"):
        raise PromotionError("uploaded processor checkpoint belongs to another generation")
    unsigned_copy = dict(checkpoint_copy)
    claimed_copy_sha = unsigned_copy.pop("checkpoint_sha256", None)
    if claimed_copy_sha != hashlib.sha256(canonical_json(unsigned_copy)).hexdigest():
        raise PromotionError("uploaded processor checkpoint receipt digest is invalid")
    checkpoint_copy_locator = checkpoint_copy.get("output_artifact")
    if not isinstance(checkpoint_copy_locator, Mapping):
        raise PromotionError("uploaded processor checkpoint receipt has no output artifact locator")
    if checkpoint_copy_locator.get("artifact_id") is not None:
        raise PromotionError("uploaded processor checkpoint already has a bound artifact id")
    copied_expiry = parse_utc_timestamp(checkpoint_copy_locator.get("expires_at"), "uploaded processor artifact expiry")
    final_expiry = parse_utc_timestamp(locator.get("expires_at"), "durable processor artifact expiry")
    copied_heartbeat = parse_utc_timestamp(
        checkpoint_copy.get("last_heartbeat_at"), "uploaded processor checkpoint heartbeat",
    )
    final_heartbeat = parse_utc_timestamp(
        checkpoint.get("last_heartbeat_at"), "durable processor checkpoint heartbeat",
    )
    observation = checkpoint.get("last_observation")
    observed_at = parse_utc_timestamp(
        observation.get("observed_at") if isinstance(observation, Mapping) else checkpoint.get("observed_at"),
        "processor generation observation",
    )
    generation_start_value = checkpoint_copy.get("observed_at") or checkpoint.get("observed_at") or observed_at.isoformat()
    generation_started_at = parse_utc_timestamp(generation_start_value, "processor generation start")
    if copied_heartbeat < generation_started_at or copied_heartbeat > final_heartbeat:
        raise PromotionError("uploaded processor checkpoint heartbeat is outside the durable generation timeline")
    if copied_expiry <= observed_at or copied_expiry > final_expiry:
        raise PromotionError("uploaded checkpoint expiry is not a valid pre-upload timestamp for the final artifact")
    uploaded_normalized = copy.deepcopy(checkpoint_copy)
    durable_normalized = copy.deepcopy(dict(checkpoint))
    uploaded_normalized.pop("checkpoint_sha256", None)
    durable_normalized.pop("checkpoint_sha256", None)
    # The producer changes only these delivery-owned fields after archiving:
    # Actions binds the artifact id/expiry, and an exact idle replay may bump
    # last_heartbeat_at. Every other checkpoint value remains immutable.
    for normalized in (uploaded_normalized, durable_normalized):
        normalized["last_heartbeat_at"] = None
        output_artifact = normalized.get("output_artifact")
        if isinstance(output_artifact, dict):
            output_artifact["artifact_id"] = None
            output_artifact["expires_at"] = None
    if uploaded_normalized != durable_normalized:
        raise PromotionError("uploaded processor checkpoint receipt differs from immutable durable generation state")
    contract_diagnostics: list[dict[str, Any]] = []
    if root is not None:
        # Keep schema and unresolved-link checks on every terminal processor
        # status. Only the Seoul unchanged-decision check needs the resolved
        # composition-baseline identity below.
        contract_diagnostics = validate_processor_link_metadata(
            checkpoint, bundle_dir, root=root, validate_seoul=False,
        )
    result = load_object(bundle_dir / "upstream-catalogue-processing-result.json")
    if result.get("generation_id") != checkpoint.get("generation_id") or result.get("status") != processor_status:
        raise PromotionError("uploaded processor result does not confirm the exact terminal generation state")
    observation = checkpoint.get("last_observation")
    locator_name = str(locator.get("name", ""))
    locator_processor_id = locator_name.removeprefix(PROCESSING_PREFIX)
    if (
        result.get("source_id") != checkpoint.get("source_id")
        or result.get("producer_run_id") != (observation.get("producer_run_id") if isinstance(observation, dict) else None)
        or result.get("processor_run_id") != locator_processor_id
        or result.get("processor_artifact_run_id") != locator.get("run_id")
        or result.get("processing_replay") is not False
        or result.get("candidate_available") is not (processor_status == "ready")
    ):
        raise PromotionError("uploaded processor result does not bind the checkpoint's exact source and run identities")
    outcome = checkpoint.get("outcome")
    if not isinstance(outcome, dict) or result.get("reason") != outcome.get("reason"):
        raise PromotionError("uploaded processor result reason differs from its durable checkpoint")
    if processor_status in {"retry", "quarantined"}:
        return {
            "status": processor_status,
            "generation_id": checkpoint["generation_id"],
            "reason": result.get("reason"),
            "contract_diagnostics": contract_diagnostics,
        }
    candidate_path = bundle_dir / "composed-candidate.registry.json"
    candidate_sha = file_sha256(candidate_path)
    receipt_path = bundle_dir / "composition-receipt.json"
    composition = load_object(receipt_path)
    generation_inputs = checkpoint.get("generation_inputs", {})
    original_baseline_sha = generation_inputs.get("baseline_sha256")
    derivation_value = generation_inputs.get("same_observation_derivation")
    composition_baseline_sha = original_baseline_sha
    if derivation_value is not None:
        derivation_helper = load_module(
            (root / "scripts/upstream_catalogue_derivation.py") if root is not None
            else pathlib.Path(__file__).with_name("upstream_catalogue_derivation.py"),
            "promotion_same_observation_derivation",
        )
        try:
            derivation_value = derivation_helper.validate_derivation_envelope(derivation_value)
        except (ValueError, TypeError) as exc:
            raise PromotionError("processor same-observation derivation envelope is invalid") from exc
        composition_baseline_sha = derivation_value["composition_baseline"]["registry_sha256"]
        composition_baseline_bytes = derivation_value["composition_baseline"]["registry_bytes"]
        if (
            derivation_value["original_observation"]["original_baseline_sha256"] != original_baseline_sha
            or derivation_value["original_observation"]["candidate_sha256"] != generation_inputs.get("candidate_sha256")
        ):
            raise PromotionError("processor derivation does not preserve its original source observation")
    else:
        composition_baseline_bytes = None
    baseline_sha = composition_baseline_sha
    producer_candidate_sha = generation_inputs.get("candidate_sha256")
    input_digests = composition.get("input_digests")
    if not isinstance(input_digests, Mapping):
        raise PromotionError("composition receipt lacks exact baseline and candidate input digests")
    for receipt_key, label, digest in (
        ("baseline", "original baseline", original_baseline_sha),
        ("candidate", "upstream candidate", producer_candidate_sha),
    ):
        observed = input_digests.get(receipt_key)
        if (
            not isinstance(digest, str)
            or not isinstance(observed, Mapping)
            or observed.get("sha256") != digest
        ):
            raise PromotionError(f"composition receipt is not bound to the exact {label} digest in processor state")
    composition_baseline_input = input_digests.get("composition_baseline")
    if derivation_value is not None:
        if (
            not isinstance(composition_baseline_input, Mapping)
            or composition_baseline_input.get("sha256") != composition_baseline_sha
            or composition_baseline_input.get("bytes") != composition_baseline_bytes
            or composition.get("same_observation_derivation") != derivation_value
        ):
            raise PromotionError("composition receipt does not bind the exact derivative baseline and lineage")
    elif isinstance(composition_baseline_input, Mapping):
        raw_size = composition_baseline_input.get("bytes")
        if (
            composition_baseline_input.get("sha256") != composition_baseline_sha
            or isinstance(raw_size, bool) or not isinstance(raw_size, int) or raw_size < 1
        ):
            raise PromotionError("composition receipt has an invalid exact composition-baseline size")
        composition_baseline_bytes = raw_size
    else:
        baseline_input = input_digests.get("baseline")
        raw_size = baseline_input.get("bytes") if isinstance(baseline_input, Mapping) else None
        if (
            not isinstance(baseline_input, Mapping)
            or baseline_input.get("sha256") != composition_baseline_sha
            or isinstance(raw_size, bool) or not isinstance(raw_size, int) or raw_size < 1
        ):
            raise PromotionError("composition receipt has no exact composition-baseline size")
        composition_baseline_bytes = raw_size
    expected_composition_status = "ready_scoped" if processor_status == "ready" else "no_change"
    try:
        composition_helper.validate_composition(
            composition, bundle_dir, composition_schema, candidate_sha,
            expected_status=expected_composition_status,
        )
    except Exception as exc:
        raise PromotionError(f"composer did not admit a valid {expected_composition_status} candidate receipt") from exc
    diagnostic_encoding = "projected"
    if contract_diagnostics and root is not None and producer_head_sha is not None:
        producer_composer = processor_composer_source_identity(root, producer_head_sha)
        diagnostic_encoding = authenticate_contract_diagnostic_encoding(composition, producer_composer)
    if root is not None or contract_diagnostics:
        validate_processor_contract_diagnostic_outputs(
            bundle_dir, contract_diagnostics, encoding=diagnostic_encoding,
        )
    outcome = checkpoint.get("outcome", {})
    if processor_status == "no-change":
        if candidate_sha != composition_baseline_sha or outcome.get("composer_status") != "no_change" or int(outcome.get("pending_count", -1)) != 0 or int(outcome.get("detail_retry_count", -1)) != 0:
            raise PromotionError("no-change processor proof does not bind an unchanged candidate with zero pending work")
    result = {
        "status": processor_status,
        "registry_path": "data/data-go-kr.registry.json",
        "registry_bytes": candidate_path.stat().st_size,
        "registry_sha256": candidate_sha,
        # Preserve the legacy field for existing callers, while carrying the
        # two source identities separately through the C validation path.
        "baseline_sha256": composition_baseline_sha,
        "composition_baseline_sha256": composition_baseline_sha,
        "composition_baseline_bytes": composition_baseline_bytes,
        "original_baseline_sha256": original_baseline_sha,
        "producer_candidate_sha256": producer_candidate_sha,
        "composition_receipt": composition,
        "composition_receipt_path": str(receipt_path.resolve()),
        "composition_receipt_sha256": file_sha256(receipt_path),
        "composition_outputs_dir": str(bundle_dir.resolve()),
        "contract_diagnostics": contract_diagnostics,
    }
    if root is not None and not defer_seoul_declaration:
        validate_processor_seoul_bundle(
            checkpoint, bundle_dir, result, root=root,
            canonical_context=canonical_context,
            allow_terminal_noop=allow_terminal_noop,
        )
    return result


def screen_processor_recovery_candidate(
    root: pathlib.Path,
    repository: str,
    checkpoint: Mapping[str, Any],
    *,
    default_branch: str,
    current_head_sha: str,
    composition_schema: Mapping[str, Any],
    composition_helper: Any,
    canonical_context: Mapping[str, Any] | None = None,
    state_root: pathlib.Path | None = None,
    journal: Mapping[str, Any] | None = None,
    journal_ref_sha: str | None = None,
) -> tuple[dict[str, Any] | None, str | None]:
    """Validate one durable generation completely before it can occupy this C run."""
    generation_id = str(checkpoint.get("generation_id", ""))
    run_id, attempt, _name = processor_attempt_from_locator(checkpoint)
    try:
        run = processor_run_api(root, repository, run_id, attempt)
    except ProcessorCandidateError:
        return None, "processor_run_unavailable"
    try:
        run = validate_trusted_processor_run(
            run, repository=repository, run_id=run_id, attempt=attempt,
            default_branch=default_branch,
        )
    except PromotionError:
        return None, "processor_run_provenance_invalid"

    artifact_id = str(checkpoint["output_artifact"].get("artifact_id", ""))
    try:
        artifact = processor_artifact_api(root, repository, run_id, artifact_id)
    except ProcessorCandidateError:
        return None, "processor_artifact_listing_unavailable"
    if artifact is None:
        return None, "processor_artifact_unavailable"
    try:
        artifact = validate_processor_artifact_metadata(
            artifact, checkpoint, run, repository=repository,
        )
    except PromotionError:
        return None, "processor_artifact_metadata_invalid_or_expired"

    try:
        bundle_dir = download_processor_artifact(
            root, repository, artifact, root / ".datapan/recovered-processor-output",
        )
    except ProcessorCandidateError:
        return None, "processor_artifact_bundle_invalid"
    try:
        bundle = validate_processor_bundle(
            checkpoint, bundle_dir, composition_schema, composition_helper, root=root,
            producer_head_sha=str(run["head_sha"]),
            canonical_context=canonical_context,
            allow_terminal_noop=True,
            defer_seoul_declaration=True,
        )
        verify_processor_input_compatibility(
            root, checkpoint, str(run["head_sha"]), current_head_sha,
            composition_receipt=bundle.get("composition_receipt"),
        )
        canonical_identity = canonical_context.get("identity") if isinstance(canonical_context, Mapping) else None
        terminal_already_canonical = (
            isinstance(canonical_identity, Mapping)
            and bundle_matches_current_canonical(bundle, canonical_identity)
        )
        if terminal_already_canonical:
            # The immutable bundle, compatibility inputs, and Seoul semantics
            # are still checked. Exact current payload identity makes this a
            # no-write terminal observation, so an old derivation's current-C
            # lineage gate must not turn an already-delivered payload into a
            # stale candidate.
            validate_processor_seoul_bundle(
                checkpoint, bundle_dir, bundle, root=root,
                canonical_context=canonical_context,
                allow_terminal_noop=True,
            )
        else:
            derivation = checkpoint.get("generation_inputs", {}).get("same_observation_derivation")
            if derivation is not None:
                if state_root is None or not isinstance(canonical_identity, Mapping):
                    raise PromotionError("same-observation derivation lacks authenticated C context")
                validate_same_observation_derivation_for_c(
                    root, state_root, checkpoint, bundle, journal, journal_ref_sha,
                    current_head_sha=current_head_sha,
                    canonical_identity=canonical_identity,
                    allow_terminal_noop=False,
                )
            validate_processor_seoul_bundle(
                checkpoint, bundle_dir, bundle, root=root,
                canonical_context=canonical_context,
                allow_terminal_noop=False,
            )
    except PromotionError:
        return None, "processor_bundle_or_input_contract_incompatible"
    return {
        "run_id": run_id,
        "attempt": attempt,
        "run": run,
        "artifact_id": artifact_id,
        "bundle_dir": bundle_dir,
        "bundle": bundle,
        "generation_id": generation_id,
        "canonical_context": canonical_context,
    }, None


MAX_TERMINAL_OUTCOME_GENERATIONS = 64
TERMINAL_OUTCOME_SCHEMA_VERSION = "datapan.canonical-update-promotion-terminal-outcome.v1"
TERMINAL_OUTCOME_WORKFLOW_PATH = ".github/workflows/canonical-update-promotion.yml"
TERMINAL_OUTCOME_SCHEMA_PATH = "schemas/datapan.canonical-update-promotion-terminal-outcome.v1.schema.json"
TERMINAL_OUTCOME_ROOT = pathlib.PurePosixPath(".datapan/ci/canonical-update-promotion-outcomes")
TERMINAL_EVALUATOR_SOURCE_PATHS = (
    ".github/workflows/canonical-update-promotion.yml",
    "scripts/run-canonical-update-promotion.py",
    "scripts/canonical_update_pr.py",
    "scripts/canonical_update_ci.py",
    "scripts/refresh-canonical-snapshot-evidence.py",
    "scripts/upstream-catalogue-state-branch.py",
    "scripts/upstream_catalogue_handoff.py",
    "scripts/compose-upstream-catalogue-candidate.py",
    "scripts/generate-batch-link-detail-registry-patches.py",
    "scripts/seoul_oa109_operation_declaration.py",
    "scripts/upstream_catalogue_derivation.py",
    "scripts/materialize-canonical-registry.py",
    "scripts/check-upstream-catalogue-health.py",
    "scripts/canonical_update_terminal_evidence.py",
    "schemas/datapan.canonical-update-promotion-terminal-outcome.v1.schema.json",
    "schemas/datapan.upstream-catalogue-checkpoint.v1.schema.json",
    "schemas/datapan.catalogue-composition-receipt.v1.schema.json",
    "schemas/datapan.catalogue-enrichment-evidence.v1.schema.json",
    "schemas/datapan.specs.v1.schema.json",
    "schemas/datapan.provider-index.v1.schema.json",
    "schemas/datapan.catalog-diff.v1.schema.json",
    "schemas/datapan.upstream-refresh-evidence.v1.schema.json",
    "schemas/datapan.canonical-update-promotion-journal.v1.schema.json",
    "schemas/datapan.canonical-update-promotion-receipt.v1.schema.json",
    "policy/upstream-catalogue-health.json",
)


def terminal_generation_record(
    checkpoint: Mapping[str, Any], *, status: str, reason_code: str | None,
    screened: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Project bounded source and artifact identities for one screened B generation."""
    locator = checkpoint.get("output_artifact") if isinstance(checkpoint.get("output_artifact"), Mapping) else {}
    name_match = re.fullmatch(
        r"upstream-catalogue-processing-([0-9]{6,20})-([1-9][0-9]*)",
        str(locator.get("name", "")),
    )
    run_id = str(locator.get("run_id", ""))
    run_attempt = int(name_match.group(2)) if name_match and name_match.group(1) == run_id else None
    screened_run = screened.get("run") if isinstance(screened, Mapping) and isinstance(screened.get("run"), Mapping) else {}
    bundle = screened.get("bundle") if isinstance(screened, Mapping) and isinstance(screened.get("bundle"), Mapping) else {}
    observation = checkpoint.get("last_observation") if isinstance(checkpoint.get("last_observation"), Mapping) else {}
    outcome = checkpoint.get("outcome") if isinstance(checkpoint.get("outcome"), Mapping) else {}
    producer = {
        "run_id": run_id if re.fullmatch(r"[0-9]{6,20}", run_id) else None,
        "run_attempt": run_attempt,
        "head_sha": screened_run.get("head_sha") if re.fullmatch(r"[a-f0-9]{40}", str(screened_run.get("head_sha", ""))) else None,
        "artifact_id": str(locator.get("artifact_id")) if re.fullmatch(r"[0-9]{1,20}", str(locator.get("artifact_id", ""))) else None,
        "artifact_name": locator.get("name") if isinstance(locator.get("name"), str) else None,
        "artifact_expires_at": locator.get("expires_at") if isinstance(locator.get("expires_at"), str) else None,
        "bundle_manifest_sha256": locator.get("bundle_manifest_sha256") if re.fullmatch(r"[a-f0-9]{64}", str(locator.get("bundle_manifest_sha256", ""))) else None,
    }
    composition = None
    if isinstance(bundle, Mapping):
        registry_path = bundle.get("registry_path")
        registry_bytes = bundle.get("registry_bytes")
        registry_sha = bundle.get("registry_sha256")
        if (
            isinstance(registry_path, str)
            and type(registry_bytes) is int and registry_bytes > 0
            and isinstance(registry_sha, str) and re.fullmatch(r"[a-f0-9]{64}", registry_sha)
        ):
            composition = {
                "baseline_sha256": bundle.get("composition_baseline_sha256", bundle.get("baseline_sha256")),
                "registry_path": registry_path,
                "registry_bytes": registry_bytes,
                "registry_sha256": registry_sha,
                "composition_receipt_sha256": bundle.get("composition_receipt_sha256"),
            }
            for key in ("baseline_sha256", "composition_receipt_sha256"):
                value = composition.get(key)
                if not isinstance(value, str) or not re.fullmatch(r"[a-f0-9]{64}", value):
                    composition[key] = None
    checkpoint_observation = None
    if isinstance(observation, Mapping):
        observed_at = observation.get("observed_at")
        producer_run_id = observation.get("producer_run_id")
        refresh_sha = observation.get("refresh_evidence_sha256")
        collection_status = observation.get("collection_status")
        execution_mode = observation.get("execution_mode")
        if (
            isinstance(observed_at, str)
            and isinstance(producer_run_id, str)
            and collection_status in {"success", "failure", "missing"}
            and execution_mode in {"live", "fixture"}
        ):
            checkpoint_observation = {
                "observed_at": observed_at,
                "producer_run_id": producer_run_id,
                "refresh_evidence_sha256": refresh_sha if refresh_sha is None or re.fullmatch(r"[a-f0-9]{64}", str(refresh_sha)) else None,
                "collection_status": collection_status,
                "execution_mode": execution_mode,
                "claim": "checkpoint_reported_last_observation",
            }
    def optional_count(key: str) -> int | None:
        value = outcome.get(key)
        return value if type(value) is int and value >= 0 else None
    safe_reason = reason_code if isinstance(reason_code, str) and re.fullmatch(r"[a-z0-9_]{1,96}", reason_code) else None
    return {
        "generation_id": checkpoint.get("generation_id") if re.fullmatch(r"[a-f0-9]{64}", str(checkpoint.get("generation_id", ""))) else None,
        "checkpoint_sha256": checkpoint.get("checkpoint_sha256") if re.fullmatch(r"[a-f0-9]{64}", str(checkpoint.get("checkpoint_sha256", ""))) else None,
        "source_id": checkpoint.get("source_id") if isinstance(checkpoint.get("source_id"), str) else None,
        "source_scope": checkpoint.get("source_scope") if isinstance(checkpoint.get("source_scope"), str) else None,
        "status": status,
        "reason_code": safe_reason,
        "producer": producer,
        "checkpoint_observation": checkpoint_observation,
        "checkpoint_observation_count": (
            checkpoint.get("observation_count")
            if type(checkpoint.get("observation_count")) is int and checkpoint.get("observation_count") >= 1
            else None
        ),
        "generation_baseline_sha256": (
            checkpoint.get("generation_inputs", {}).get("baseline_sha256")
            if isinstance(checkpoint.get("generation_inputs"), Mapping)
            and re.fullmatch(r"[a-f0-9]{64}", str(checkpoint.get("generation_inputs", {}).get("baseline_sha256", "")))
            else None
        ),
        "checkpoint_derivation_sha256": (
            hashlib.sha256(canonical_json(checkpoint.get("generation_inputs", {}).get("same_observation_derivation"))).hexdigest()
            if isinstance(checkpoint.get("generation_inputs"), Mapping)
            and isinstance(checkpoint.get("generation_inputs", {}).get("same_observation_derivation"), Mapping)
            else None
        ),
        "composition": composition,
        "pending_count": optional_count("pending_count"),
        "detail_retry_count": optional_count("detail_retry_count"),
        "detail_unattempted_count": optional_count("detail_unattempted_count"),
    }


def terminal_recovery_result(
    *, status: str, candidate_available: bool, generations: Sequence[Mapping[str, Any]],
    evaluated_main: Mapping[str, Any] | None = None, selected_generation_id: str | None = None,
    preparation_returned: bool = False,
) -> dict[str, Any]:
    rows = list(generations[:MAX_TERMINAL_OUTCOME_GENERATIONS])
    return {
        "kind": "recover-ready",
        "status": status,
        "candidate_available": candidate_available,
        "evaluated_main": dict(evaluated_main) if isinstance(evaluated_main, Mapping) else None,
        "selected_generation_id": selected_generation_id,
        "preparation_returned": preparation_returned,
        "generation_count": len(generations),
        "generation_results_truncated": len(generations) > len(rows),
        "generations": rows,
    }


def terminal_main_identity(identity: Mapping[str, Any] | None) -> dict[str, Any] | None:
    """Project only the authenticated C main identity into the versioned receipt shape."""
    if not isinstance(identity, Mapping):
        return None
    revision = identity.get("main_sha", identity.get("revision"))
    manifest_sha = identity.get("manifest_sha256")
    path = identity.get("registry_path")
    byte_count = identity.get("registry_bytes")
    registry_sha = identity.get("registry_sha256")
    if (
        not isinstance(revision, str) or not re.fullmatch(r"[a-f0-9]{40}", revision)
        or not isinstance(manifest_sha, str) or not re.fullmatch(r"[a-f0-9]{64}", manifest_sha)
        or not isinstance(path, str) or not path
        or type(byte_count) is not int or byte_count < 1
        or not isinstance(registry_sha, str) or not re.fullmatch(r"[a-f0-9]{64}", registry_sha)
    ):
        return None
    return {
        "revision": revision,
        "manifest_sha256": manifest_sha,
        "registry_path": path,
        "registry_bytes": byte_count,
        "registry_sha256": registry_sha,
    }


def terminal_mode_result(mode: str) -> dict[str, Any]:
    """Use one strict result shape for non-recovery invocations without inventing recovery facts."""
    return {
        "kind": mode,
        "status": "mode_completed_without_recovery_outcome",
        "candidate_available": False,
        "evaluated_main": None,
        "selected_generation_id": None,
        "preparation_returned": False,
        "generation_count": 0,
        "generation_results_truncated": False,
        "generations": [],
    }


def terminal_utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def terminal_execution_failure_code(exc: Exception) -> str:
    if isinstance(exc, PromotionError):
        message = str(exc)
        if message.startswith("stale_base:"):
            return "stale_base_revalidation_required"
        if message.startswith("terminal_outcome_evaluator_source_mismatch"):
            return "evaluator_source_mismatch"
        if message.startswith("terminal_outcome"):
            return "terminal_outcome_contract_invalid"
    return "controlled_execution_failure"


class TerminalOutcomeRecorder:
    """Write one attempt-bound C execution record; never overwrite a prior attempt path."""

    def __init__(
        self, root: pathlib.Path, output: pathlib.Path, *, repository: str,
        mode: str, evaluator_source_sha: str, run_id: str, run_attempt: str,
        event_name: str,
    ) -> None:
        if (
            not re.fullmatch(r"[a-f0-9]{40}", evaluator_source_sha)
            or not re.fullmatch(r"[0-9]{1,20}", run_id)
            or not re.fullmatch(r"[1-9][0-9]{0,8}", run_attempt)
            or mode not in {"recover-ready", "reconcile-prs", "refresh-owned-source", "reconcile-publication"}
            or event_name not in {"schedule", "workflow_run", "workflow_dispatch", "local"}
            or not re.fullmatch(r"[^\s/]+/[^\s/]+", repository)
        ):
            raise PromotionError("terminal_outcome_invocation_identity_invalid")
        root = root.resolve(strict=True)
        candidate = output if output.is_absolute() else root / output
        resolved_parent = candidate.parent.resolve(strict=False)
        try:
            relative_parent = resolved_parent.relative_to(root)
        except ValueError as exc:
            raise PromotionError("terminal_outcome_output_outside_repository") from exc
        expected_parent = pathlib.PurePosixPath(
            *TERMINAL_OUTCOME_ROOT.parts, run_id, str(int(run_attempt)), mode,
        )
        if (
            tuple(relative_parent.parts[:len(TERMINAL_OUTCOME_ROOT.parts)]) != tuple(TERMINAL_OUTCOME_ROOT.parts)
            or pathlib.PurePosixPath(*relative_parent.parts) != expected_parent
            or candidate.name != "terminal-outcome.json"
            or any((root.joinpath(*relative_parent.parts[:index])).is_symlink() for index in range(1, len(relative_parent.parts) + 1))
        ):
            raise PromotionError("terminal_outcome_output_path_invalid")
        if resolved_parent.exists():
            if not resolved_parent.is_dir() or any(resolved_parent.iterdir()):
                raise PromotionError("terminal_outcome_stale_output_present")
            resolved_parent.chmod(0o700)
        else:
            resolved_parent.mkdir(parents=True, mode=0o700, exist_ok=False)
        output_path = resolved_parent / "terminal-outcome.json"
        working_head_sha = command(("git", "rev-parse", "HEAD"), root).stdout.strip()
        if not re.fullmatch(r"[a-f0-9]{40}", working_head_sha):
            raise PromotionError("terminal_outcome_checkout_identity_invalid")
        initial_checkout_sha = os.environ.get("C_INITIAL_CHECKOUT_SHA", evaluator_source_sha)
        if not re.fullmatch(r"[a-f0-9]{40}", initial_checkout_sha):
            raise PromotionError("terminal_outcome_initial_checkout_identity_invalid")
        source_files_match = initial_checkout_sha == evaluator_source_sha
        trusted_schema: dict[str, Any] | None = None
        if source_files_match:
            for relative in TERMINAL_EVALUATOR_SOURCE_PATHS:
                try:
                    current = root.joinpath(*pathlib.PurePosixPath(relative).parts).read_bytes()
                    expected = subprocess.run(
                        ("git", "show", f"{evaluator_source_sha}:{relative}"),
                        cwd=root, capture_output=True, check=False, timeout=10,
                    )
                except (OSError, subprocess.TimeoutExpired):
                    source_files_match = False
                    break
                if expected.returncode != 0 or expected.stdout != current:
                    source_files_match = False
                    break
                if relative == TERMINAL_OUTCOME_SCHEMA_PATH:
                    try:
                        loaded_schema = json.loads(expected.stdout)
                    except (UnicodeDecodeError, json.JSONDecodeError):
                        source_files_match = False
                        break
                    if not isinstance(loaded_schema, dict):
                        source_files_match = False
                        break
                    trusted_schema = loaded_schema
        if trusted_schema is None:
            try:
                schema_result = subprocess.run(
                    ("git", "show", f"{evaluator_source_sha}:{TERMINAL_OUTCOME_SCHEMA_PATH}"),
                    cwd=root, capture_output=True, check=False, timeout=10,
                )
                trusted_schema = json.loads(schema_result.stdout) if schema_result.returncode == 0 else None
            except (OSError, subprocess.TimeoutExpired, UnicodeDecodeError, json.JSONDecodeError):
                trusted_schema = None
        if not isinstance(trusted_schema, dict):
            raise PromotionError("terminal_outcome_schema_unavailable_at_evaluator_source")
        self.path = output_path
        self.sha_path = resolved_parent / "terminal-outcome.sha256"
        self.root = root
        self.schema = trusted_schema
        self.source_matches = source_files_match
        self._output_initialized = False
        self.document: dict[str, Any] = {
            "schema_version": TERMINAL_OUTCOME_SCHEMA_VERSION,
            "repository": repository,
            "workflow_path": TERMINAL_OUTCOME_WORKFLOW_PATH,
            "invocation": {
                "mode": mode,
                "run_id": run_id,
                "run_attempt": int(run_attempt),
                "event": event_name,
            },
            "evaluator": {
                "source_sha": evaluator_source_sha,
                "workflow_checkout_sha": initial_checkout_sha,
                "working_tree_head_sha": working_head_sha,
                "checkout_matches_source": initial_checkout_sha == evaluator_source_sha,
                "loaded_files_match_source": source_files_match,
            },
            "execution_status": "started",
            "started_at": terminal_utc_now(),
            "completed_at": None,
            "failure_code": None,
            "outcome": None,
        }
        self._write_document()

    def _write_document(self) -> None:
        import jsonschema

        jsonschema.Draft202012Validator(self.schema, format_checker=jsonschema.FormatChecker()).validate(self.document)
        payload = canonical_json(self.document) + b"\n"
        temporary = self.path.with_name(".terminal-outcome.tmp")
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            temporary.chmod(0o600)
            if self._output_initialized:
                os.replace(temporary, self.path)
            else:
                os.link(temporary, self.path)
                temporary.unlink()
                self._output_initialized = True
            self.path.chmod(0o600)
        finally:
            temporary.unlink(missing_ok=True)

    def finish(self, outcome: Mapping[str, Any]) -> None:
        self.document.update({
            "execution_status": "completed",
            "completed_at": terminal_utc_now(),
            "outcome": dict(outcome),
        })
        self._write_document()
        digest_line = f"{hashlib.sha256(self.path.read_bytes()).hexdigest()}  terminal-outcome.json\n".encode("ascii")
        fd = os.open(self.sha_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(digest_line)
                stream.flush()
                os.fsync(stream.fileno())
            self.sha_path.chmod(0o600)
        except BaseException:
            self.sha_path.unlink(missing_ok=True)
            raise

    def fail(self, reason_code: str) -> None:
        stable_reason = reason_code if re.fullmatch(r"[a-z0-9_]{1,96}", reason_code) else "controlled_execution_failure"
        self.document.update({
            "execution_status": "failed",
            "completed_at": terminal_utc_now(),
            "failure_code": stable_reason,
            "outcome": None,
        })
        self._write_document()
        digest_line = f"{hashlib.sha256(self.path.read_bytes()).hexdigest()}  terminal-outcome.json\n".encode("ascii")
        fd = os.open(self.sha_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as stream:
            stream.write(digest_line)
            stream.flush()
            os.fsync(stream.fileno())
        self.sha_path.chmod(0o600)


def select_first_eligible_processor_bundle(
    root: pathlib.Path,
    repository: str,
    candidates: Sequence[Mapping[str, Any]],
    blocked: list[dict[str, str]],
    journal: Mapping[str, Any] | None = None,
    journal_ref_sha: str | None = None,
    state_root: pathlib.Path | None = None,
    *,
    default_branch: str,
    current_head_sha: str,
    composition_schema: Mapping[str, Any],
    composition_helper: Any,
    already_canonical: list[dict[str, Any]] | None = None,
    terminal_generations: list[dict[str, Any]] | None = None,
    terminal_context: dict[str, Any] | None = None,
) -> tuple[dict[str, Any] | None, list[dict[str, str]]]:
    """Screen generations in observation order and stop at the first valid bundle."""
    already_canonical_rows = already_canonical if already_canonical is not None else []
    needs_canonical_context = any(
        isinstance(checkpoint, Mapping) and checkpoint.get("status") in {"ready", "no-change"}
        for checkpoint in candidates
    )
    canonical_context = (
        authenticated_current_canonical_context(root, current_head_sha)
        if needs_canonical_context else None
    )
    canonical_identity = (
        canonical_context.get("identity") if isinstance(canonical_context, Mapping) else None
    )
    if terminal_context is not None and isinstance(canonical_identity, Mapping):
        terminal_context["evaluated_main"] = terminal_main_identity(canonical_identity)
    terminal_rows = terminal_generations if terminal_generations is not None else []
    for checkpoint in candidates:
        generation_id = str(checkpoint.get("generation_id", ""))
        screened, reason = screen_processor_recovery_candidate(
            root, repository, checkpoint,
            default_branch=default_branch,
            current_head_sha=current_head_sha,
            composition_schema=composition_schema,
            composition_helper=composition_helper,
            canonical_context=canonical_context,
            state_root=state_root,
            journal=journal,
            journal_ref_sha=journal_ref_sha,
        )
        if screened is None:
            blocked.append({"generation_id": generation_id, "reason": str(reason)})
            terminal_rows.append(terminal_generation_record(
                checkpoint, status="blocked",
                reason_code=reason if isinstance(reason, str) else "processor_screen_rejected",
            ))
            continue
        bundle = screened.get("bundle", {})
        if not isinstance(canonical_identity, Mapping):
            raise PromotionError("authenticated current-canonical context is missing during processor selection")
        if bundle_matches_current_canonical(bundle, canonical_identity):
            # A fully screened immutable B artifact whose exact composed bytes
            # are already the manifest-bound current canonical payload is a
            # no-op. Its historical derivation baseline is expected to differ
            # after the ordinary C merge, so do not reinterpret that old
            # parent proof as authority to prepare another candidate.
            outcome = checkpoint.get("outcome", {})
            already_canonical_rows.append({
                "generation_id": generation_id,
                "reason": "already_canonical_payload",
                "registry_sha256": str(bundle.get("registry_sha256", "")),
                "pending_count": outcome.get("pending_count"),
                "detail_retry_count": outcome.get("detail_retry_count"),
                "detail_unattempted_count": outcome.get("detail_unattempted_count"),
                "candidate_available": False,
            })
            terminal_rows.append(terminal_generation_record(
                checkpoint, status="already_canonical", reason_code="already_canonical_payload",
                screened=screened,
            ))
            continue
        if bundle.get("baseline_sha256") != canonical_identity.get("registry_sha256"):
            # A different payload composed from an older immutable baseline
            # cannot be rebased safely. Keep it visible as blocked, then let a
            # later fresh observation compete for selection.
            blocked.append({
                "generation_id": generation_id,
                "reason": "processor_baseline_stale_for_current_canonical",
            })
            terminal_rows.append(terminal_generation_record(
                checkpoint, status="blocked", reason_code="processor_baseline_stale_for_current_canonical",
                screened=screened,
            ))
            continue
        already_active_payload = any(
            isinstance(row, Mapping)
            and row.get("superseded_by") is None
            and row.get("status") == "pending-review"
            and str(row.get("candidate", {}).get("repository", "")).casefold() == repository.casefold()
            and row.get("candidate", {}).get("source_id") == checkpoint.get("source_id")
            and row.get("candidate", {}).get("scope") == checkpoint.get("source_scope")
            and row.get("candidate", {}).get("registry_sha256") == bundle.get("registry_sha256")
            for row in (journal.get("records", []) if isinstance(journal, Mapping) else [])
        )
        if already_active_payload:
            # Another B observation may have a distinct generation while
            # composing the same bytes. It is already represented by this
            # active PR, so don't let that no-op starve a later candidate.
            terminal_rows.append(terminal_generation_record(
                checkpoint, status="already_represented", reason_code="active_promotion_payload_exists",
                screened=screened,
            ))
            continue
        revision = journal_record_for(
            journal,
            str(checkpoint.get("source_id", "")),
            str(checkpoint.get("source_scope", "")),
            generation_id,
            str(bundle.get("registry_sha256", "")),
        )
        if revision is not None and revision.get("superseded_by") is not None:
            terminal_rows.append(terminal_generation_record(
                checkpoint, status="already_represented", reason_code="generation_superseded",
                screened=screened,
            ))
            continue
        if revision is not None and revision.get("status") != "prepared":
            # Exact payload redelivery is idempotent, even if the producer's
            # heartbeat or Actions artifact locator has advanced.
            terminal_rows.append(terminal_generation_record(
                checkpoint, status="already_represented", reason_code="generation_already_recorded",
                screened=screened,
            ))
            continue
        terminal_rows.append(terminal_generation_record(
            checkpoint, status="selected", reason_code=None, screened=screened,
        ))
        screened["prior_revision"] = revision
        return screened, blocked
    return None, blocked


def registry_sha_from_worktree(root: pathlib.Path) -> tuple[int, str]:
    path = root / "data/data-go-kr.registry.json"
    return registry_sha_from_path(path)


def registry_sha_from_path(path: pathlib.Path) -> tuple[int, str]:
    data = path.read_bytes()
    if data.startswith(b"version https://git-lfs.github.com/spec/v1"):
        raise PromotionError("baseline registry did not materialize from its declared immutable source")
    try:
        value = json.loads(data)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PromotionError("baseline registry is not valid JSON") from exc
    if not isinstance(value, list) or not value:
        raise PromotionError("baseline registry is not a nonempty canonical array")
    return len(data), hashlib.sha256(data).hexdigest()


def authenticated_current_canonical_registry(
    root: pathlib.Path,
    expected_main_sha: str,
) -> dict[str, Any]:
    """Authenticate the current canonical bytes against the pinned main manifest."""
    if not re.fullmatch(r"[a-f0-9]{40}", expected_main_sha):
        raise PromotionError("current canonical registry check requires a full pinned main SHA")
    local_head = command(("git", "rev-parse", "HEAD"), root).stdout.strip()
    if local_head != expected_main_sha:
        raise PromotionError("stale_base: candidate checkout is not pinned to the expected main commit")
    def verify_remote_main() -> None:
        remote_main = command(("git", "ls-remote", "--heads", "origin", "refs/heads/main"), root)
        main_rows = [
            line.split("\t", 1)[0]
            for line in remote_main.stdout.splitlines()
            if line.endswith("\trefs/heads/main")
        ]
        if main_rows != [expected_main_sha]:
            raise PromotionError("stale_base: main changed after the upstream observation; request a fresh catalogue observation")

    verify_remote_main()

    manifest_path = root / "manifest.json"
    if manifest_path.is_symlink() or not manifest_path.is_file():
        raise PromotionError("pinned release manifest is not a regular working-tree file")
    working_manifest_bytes = manifest_path.read_bytes()
    committed_manifest_bytes = command(
        ("git", "show", f"{expected_main_sha}:manifest.json"), root,
    ).stdout.encode("utf-8")
    if working_manifest_bytes != committed_manifest_bytes:
        raise PromotionError("release manifest working bytes do not match the pinned main commit")
    try:
        manifest = json.loads(committed_manifest_bytes)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PromotionError("pinned release manifest is not valid JSON") from exc
    if not isinstance(manifest, Mapping):
        raise PromotionError("pinned release manifest must be a JSON object")
    registry_path = manifest.get("source_registry")
    if not isinstance(registry_path, str) or not registry_path:
        raise PromotionError("release manifest does not identify its canonical registry")
    relative = pathlib.PurePosixPath(registry_path)
    if relative.is_absolute() or ".." in relative.parts or "\\" in registry_path:
        raise PromotionError("release manifest canonical registry path is unsafe")
    registry_entries = [
        row for row in manifest.get("artifacts", [])
        if isinstance(row, Mapping)
        and row.get("path") == registry_path
        and row.get("kind") == "registry"
    ]
    if len(registry_entries) != 1:
        raise PromotionError("release manifest does not have one canonical registry artifact")
    entry = registry_entries[0]
    expected_bytes = entry.get("bytes")
    expected_sha = entry.get("sha256")
    if (
        type(expected_bytes) is not int
        or expected_bytes < 1
        or not isinstance(expected_sha, str)
        or not re.fullmatch(r"[a-f0-9]{64}", expected_sha)
    ):
        raise PromotionError("release manifest canonical registry size or digest is invalid")
    committed_pointer = command(
        ("git", "show", f"{expected_main_sha}:{registry_path}"), root,
    ).stdout.encode("utf-8")
    if parse_git_lfs_pointer_identity(committed_pointer) != (expected_sha, expected_bytes):
        raise PromotionError("pinned Git LFS registry pointer does not match the main manifest")
    canonical_path = root / ".datapan/current-canonical" / pathlib.Path(*relative.parts)
    try:
        canonical_path.resolve(strict=False).relative_to(root.resolve(strict=True))
    except (OSError, ValueError) as exc:
        raise PromotionError("current canonical registry materialization path is outside the pinned checkout") from exc
    if canonical_path.is_symlink():
        raise PromotionError("current canonical registry materialization path is a symlink")
    if not canonical_path.exists():
        manifest_sha = hashlib.sha256(committed_manifest_bytes).hexdigest()
        command((
            sys.executable,
            str(root / "scripts/materialize-canonical-registry.py"),
            "--output", str(canonical_path),
            "--backend", "github-git-lfs",
            "--candidate-commit", expected_main_sha,
            "--expected-manifest-sha256", manifest_sha,
        ), root)
    if not canonical_path.is_file() or canonical_path.is_symlink():
        raise PromotionError("current canonical registry materialization is not a regular file")
    actual_bytes, actual_sha = registry_sha_from_path(canonical_path)
    if (actual_bytes, actual_sha) != (expected_bytes, expected_sha):
        raise PromotionError("current canonical registry bytes do not match the pinned release manifest")
    verify_remote_main()
    return {
        "main_sha": expected_main_sha,
        "manifest_sha256": hashlib.sha256(committed_manifest_bytes).hexdigest(),
        "registry_path": registry_path,
        "registry_bytes": actual_bytes,
        "registry_sha256": actual_sha,
    }


def authenticated_current_canonical_context(
    root: pathlib.Path,
    expected_main_sha: str,
) -> dict[str, Any]:
    """Authenticate once, then pass the exact materialized bytes to C validators."""
    identity = authenticated_current_canonical_registry(root, expected_main_sha)
    registry_path = identity.get("registry_path")
    if not isinstance(registry_path, str) or not registry_path:
        raise PromotionError("authenticated current-canonical identity has no registry path")
    relative = pathlib.PurePosixPath(registry_path)
    if relative.is_absolute() or ".." in relative.parts or "\\" in registry_path:
        raise PromotionError("authenticated current-canonical identity has an unsafe registry path")
    canonical_path = root / ".datapan/current-canonical" / pathlib.Path(*relative.parts)
    try:
        canonical_path.resolve(strict=False).relative_to(root.resolve(strict=True))
    except (OSError, ValueError) as exc:
        raise PromotionError("authenticated current-canonical materialization escaped the trusted checkout") from exc
    if canonical_path.is_symlink() or not canonical_path.is_file():
        raise PromotionError("authenticated current-canonical materialization is not a regular file")
    raw = canonical_path.read_bytes()
    if (
        type(identity.get("registry_bytes")) is not int
        or identity.get("registry_bytes") < 1
        or len(raw) != identity.get("registry_bytes")
        or hashlib.sha256(raw).hexdigest() != identity.get("registry_sha256")
    ):
        raise PromotionError("authenticated current-canonical bytes changed after manifest verification")
    try:
        rows = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PromotionError("authenticated current-canonical bytes are not valid JSON") from exc
    if not isinstance(rows, list) or not rows:
        raise PromotionError("authenticated current-canonical bytes are not a nonempty registry array")
    return {"identity": identity, "rows": rows}


def validate_same_observation_derivation_for_c(
    root: pathlib.Path,
    state_root: pathlib.Path,
    checkpoint: Mapping[str, Any],
    bundle: Mapping[str, Any],
    journal: Mapping[str, Any] | None,
    journal_ref_sha: str | None,
    *,
    current_head_sha: str,
    canonical_identity: Mapping[str, Any],
    allow_terminal_noop: bool = False,
) -> None:
    """Recheck B's two-parent proof against the current durable C lineage.

    The composition baseline's historical manifest and current main's
    manifest are checked independently. This admits source-only main advances
    while requiring the canonical registry bytes to remain identical.
    """
    generation_inputs = checkpoint.get("generation_inputs")
    derivation_value = generation_inputs.get("same_observation_derivation") if isinstance(generation_inputs, Mapping) else None
    if derivation_value is None:
        return
    derivation_helper = load_module(root / "scripts/upstream_catalogue_derivation.py", "c_same_observation_derivation")
    try:
        envelope = derivation_helper.validate_derivation_envelope(derivation_value)
        if not isinstance(journal, Mapping) or not isinstance(journal_ref_sha, str):
            raise ValueError("current promotion journal snapshot is unavailable")
        pr_helper = load_canonical_update_pr(root)
        journal_schema = load_object(root / "schemas/datapan.canonical-update-promotion-journal.v1.schema.json")
        pr_helper.validate_journal(journal, journal_schema)
        selected_row = derivation_helper.validate_readback_against_journal(
            envelope, journal, journal_ref_sha=journal_ref_sha,
            allow_monotonic_successor=True,
        )
        health_policy = load_object(root / "policy/upstream-catalogue-health.json")
        derivation_helper.authenticate_canonical_merge_ack(
            root, str(envelope["canonical_parent_readback"]["repository"]),
            selected_row, health_policy, now=dt.datetime.now(dt.timezone.utc),
        )
        composition = bundle.get("composition_receipt")
        if not isinstance(composition, Mapping) or composition.get("same_observation_derivation") != envelope:
            raise ValueError("composition receipt derivation differs from checkpoint")
        original = envelope["original_observation"]
        baseline = envelope["composition_baseline"]
        if (
            checkpoint.get("source_id") != original["source_id"]
            or checkpoint.get("source_scope") != original["source_scope"]
            or generation_inputs.get("baseline_sha256") != original["original_baseline_sha256"]
            or generation_inputs.get("candidate_sha256") != original["candidate_sha256"]
            or generation_inputs.get("policy_sha256") != original["source_policy_sha256"]
            or generation_inputs.get("adapter_revision") != original["provider_index_sha256"]
            or bundle.get("original_baseline_sha256") != original["original_baseline_sha256"]
            or bundle.get("baseline_sha256") != baseline["registry_sha256"]
        ):
            raise ValueError("derivation original observation or effective composition baseline differs")
        composition_baseline_matches_current = (
            (baseline["registry_path"], baseline["registry_bytes"], baseline["registry_sha256"])
            == (
                canonical_identity.get("registry_path"),
                canonical_identity.get("registry_bytes"),
                canonical_identity.get("registry_sha256"),
            )
        )
        composed_candidate_matches_current = (
            allow_terminal_noop and bundle_matches_current_canonical(bundle, canonical_identity)
        )
        if not composition_baseline_matches_current and not composed_candidate_matches_current:
            raise ValueError("current canonical bytes match neither the effective baseline nor a terminal no-op output")
        if canonical_identity.get("main_sha") != current_head_sha:
            raise ValueError("current canonical identity is not pinned to checked-out main")

        # Bind the envelope's original baseline main and manifest to committed
        # Git objects. Current manifest may have advanced, but both must name
        # exactly the same materialized canonical registry.
        historical_manifest = command(
            ("git", "show", f"{baseline['main_sha']}:manifest.json"), root,
        ).stdout.encode("utf-8")
        if hashlib.sha256(historical_manifest).hexdigest() != baseline["manifest_sha256"]:
            raise ValueError("derivation baseline manifest is not the pinned historical commit")
        historical_value = json.loads(historical_manifest)
        if not isinstance(historical_value, Mapping) or historical_value.get("source_registry") != baseline["registry_path"]:
            raise ValueError("derivation baseline manifest names another canonical path")
        historical_entries = [
            row for row in historical_value.get("artifacts", [])
            if isinstance(row, Mapping) and row.get("kind") == "registry" and row.get("path") == baseline["registry_path"]
        ]
        if len(historical_entries) != 1 or (historical_entries[0].get("bytes"), historical_entries[0].get("sha256")) != (
            baseline["registry_bytes"], baseline["registry_sha256"],
        ):
            raise ValueError("derivation baseline manifest registry identity differs")
        pointer = command(("git", "show", f"{baseline['main_sha']}:{baseline['registry_path']}"), root).stdout.encode("utf-8")
        if parse_git_lfs_pointer_identity(pointer) != (baseline["registry_sha256"], baseline["registry_bytes"]):
            raise ValueError("derivation baseline LFS pointer differs")
        for ancestor, descendant in (
            (baseline["main_sha"], current_head_sha),
            (envelope["canonical_parent_readback"]["merge_sha"], baseline["main_sha"]),
            (envelope["canonical_parent_readback"]["merge_sha"], current_head_sha),
        ):
            result = command(("git", "merge-base", "--is-ancestor", ancestor, descendant), root,
                             allowed_returncodes=frozenset({0, 1}))
            if result.returncode != 0:
                raise ValueError("canonical merge or composition baseline is not in current main history")

        # Resolve both direct B parents from the durable state branch and
        # authenticate their original A subject using its admitted handoff.
        source_root = state_root / "sources" / "data_go_kr"
        index = load_object(source_root / "index.json")
        if index.get("schema_version") != PROCESSOR_SCHEMA or not isinstance(index.get("generations"), list):
            raise ValueError("processor state index is malformed")
        rows = {row.get("generation_id") for row in index["generations"] if isinstance(row, Mapping)}
        ledger = index.get("collector_handoff")
        handoff = load_module(root / "scripts/upstream_catalogue_handoff.py", "c_derivation_handoff")
        admitted = handoff.validate_ledger(ledger)["admitted_observations"] if isinstance(ledger, Mapping) else None
        if not isinstance(admitted, list):
            raise ValueError("original collector admission ledger is unavailable")

        def load_parent(reference: Mapping[str, Any]) -> dict[str, Any]:
            generation = str(reference["generation_id"])
            if generation not in rows:
                raise ValueError("derivation parent is missing from current state index")
            path = source_root / "generations" / f"{generation}.json"
            parent = verify_processor_checkpoint(load_object(path), root / "schemas/datapan.upstream-catalogue-checkpoint.v1.schema.json")
            derivation_helper.validate_processor_parent_checkpoint(
                reference, parent, original_observation=original,
            )
            observations = parent.get("last_observation") if isinstance(parent.get("last_observation"), Mapping) else {}
            input_refs = parent.get("input_artifacts") if isinstance(parent.get("input_artifacts"), list) else []
            matches = [
                item for item in admitted
                if item.get("producer_run_id") == str(observations.get("producer_run_id") or "")
                and item.get("refresh_evidence_sha256") == observations.get("refresh_evidence_sha256")
                and any(
                    isinstance(ref, Mapping)
                    and str(ref.get("run_id")) == item["producer_run_id"]
                    and str(ref.get("artifact_id")) == item["artifact_id"]
                    and ref.get("evidence_sha256") == item["refresh_evidence_sha256"]
                    for ref in input_refs
                )
            ]
            if len(matches) != 1 or derivation_helper.original_observation_from_checkpoint(parent, matches[0]) != original:
                raise ValueError("derivation B parent is not admitted to the exact original A")
            return parent

        parents = [
            load_parent(envelope["resume_parent_processor"]),
            load_parent(envelope["canonical_parent_processor"]),
        ]
        parent_id_set = {
            envelope["resume_parent_processor"]["generation_id"],
            envelope["canonical_parent_processor"]["generation_id"],
        }

        indexed_generations = {
            row.get("generation_id") for row in index.get("generations", [])
            if isinstance(row, Mapping)
        }

        def load_lineage_checkpoint(generation_id: str) -> dict[str, Any]:
            if generation_id not in indexed_generations:
                raise ValueError("derivation parent is missing from current state index")
            path = source_root / "generations" / f"{generation_id}.json"
            return verify_processor_checkpoint(
                load_object(path), root / "schemas/datapan.upstream-catalogue-checkpoint.v1.schema.json",
            )

        def admitted_observation_for(checkpoint: Mapping[str, Any]) -> Mapping[str, Any]:
            observations = checkpoint.get("last_observation") if isinstance(checkpoint.get("last_observation"), Mapping) else {}
            input_refs = checkpoint.get("input_artifacts") if isinstance(checkpoint.get("input_artifacts"), list) else []
            matches = [
                item for item in admitted
                if item.get("producer_run_id") == str(observations.get("producer_run_id") or "")
                and item.get("refresh_evidence_sha256") == observations.get("refresh_evidence_sha256")
                and any(
                    isinstance(ref, Mapping)
                    and str(ref.get("run_id")) == item["producer_run_id"]
                    and str(ref.get("artifact_id")) == item["artifact_id"]
                    and ref.get("evidence_sha256") == item["refresh_evidence_sha256"]
                    for ref in input_refs
                )
            ]
            if len(matches) != 1:
                raise ValueError("derivation B parent admission is ambiguous or missing")
            return matches[0]

        expected_ancestors = derivation_helper.validate_processor_parent_graph(
            list(parent_id_set),
            load_checkpoint=load_lineage_checkpoint,
            admission_for=admitted_observation_for,
            original_observation=original,
            expected_ancestor_generation_ids=envelope["ancestor_generation_ids"],
            forbidden_generation_id=checkpoint.get("generation_id"),
        )
        if selected_row.get("candidate", {}).get("generation_id") != envelope["canonical_parent_processor"]["generation_id"]:
            raise ValueError("canonical readback does not select its exact B producer")
        active_records = [
            row for row in journal.get("records", []) if isinstance(row, Mapping)
            and row.get("superseded_by") is None
            and row.get("status") in {"merged", "publication-pending", "published", "read-back-confirmed"}
            and isinstance(row.get("candidate"), Mapping)
            and row["candidate"].get("source_id") == original["source_id"]
            and row["candidate"].get("scope") == original["source_scope"]
            and row["candidate"].get("registry_sha256") == baseline["registry_sha256"]
        ]
        if len(active_records) != 1 or active_records[0] != selected_row:
            raise ValueError("canonical readback is ambiguous for this original observation")

        # The PR body/head/base/merge are independently read back once; the
        # frozen journal locator is not used as a substitute for GitHub state.
        readback = gh_pr_readback(root, envelope["canonical_parent_readback"]["repository"], int(envelope["canonical_parent_readback"]["pr_number"]))
        reference = envelope["canonical_parent_readback"]
        merge_commit = readback.get("mergeCommit")
        observed_merge_sha = merge_commit.get("oid") if isinstance(merge_commit, Mapping) else None
        body = readback.get("body")
        if (
            readback.get("number") != reference["pr_number"]
            or readback.get("state") != "MERGED"
            or readback.get("repository") != reference["repository"]
            or readback.get("headRepository") != reference["repository"]
            or readback.get("headRefName") != reference["pr_branch"]
            or readback.get("headRefOid") != reference["pr_head_sha"]
            or readback.get("baseRefName") != "main"
            or observed_merge_sha != reference["merge_sha"]
            or not isinstance(body, str)
            or hashlib.sha256(body.encode("utf-8")).hexdigest() != reference["pr_body_sha256"]
            or body != selected_row.get("ownership", {}).get("body")
            or readback.get("merged") is not True
        ):
            raise ValueError("canonical PR API readback differs from the exact merged journal ownership")
    except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError, PromotionError) as exc:
        raise PromotionError("processor same-observation lineage failed strict C validation") from exc


def bundle_matches_current_canonical(
    bundle: Mapping[str, Any],
    canonical_identity: Mapping[str, Any],
) -> bool:
    return (
        bundle.get("registry_path") == canonical_identity.get("registry_path")
        and type(bundle.get("registry_bytes")) is int
        and bundle.get("registry_bytes") == canonical_identity.get("registry_bytes")
        and bundle.get("registry_sha256") == canonical_identity.get("registry_sha256")
    )


def parse_git_lfs_pointer_identity(pointer: bytes) -> tuple[str, int]:
    try:
        lines = pointer.decode("ascii").splitlines()
    except UnicodeDecodeError as exc:
        raise PromotionError("pinned Git LFS registry pointer is not ASCII") from exc
    values: dict[str, str] = {}
    for line in lines:
        if " " not in line:
            continue
        key, value = line.split(" ", 1)
        if key in values:
            raise PromotionError("pinned Git LFS registry pointer contains duplicate fields")
        values[key] = value
    oid = values.get("oid", "")
    if values.get("version") != "https://git-lfs.github.com/spec/v1" or not oid.startswith("sha256:"):
        raise PromotionError("pinned main registry blob is not a supported Git LFS pointer")
    digest = oid.removeprefix("sha256:")
    if not re.fullmatch(r"[a-f0-9]{64}", digest):
        raise PromotionError("pinned Git LFS registry pointer digest is invalid")
    try:
        size = int(values.get("size", ""))
    except ValueError as exc:
        raise PromotionError("pinned Git LFS registry pointer size is invalid") from exc
    if size < 1:
        raise PromotionError("pinned Git LFS registry pointer size is invalid")
    return digest, size


def candidate_generated_file_allowlist(root: pathlib.Path, generation_id: str) -> set[str]:
    allowed = set(GENERATED_FILE_ALLOWLIST)
    allowed.update(
        (REVIEW_DIR / generation_id / name).as_posix()
        for name in ("semantic-diff.json", "regeneration-queue.json", "quarantine.json", "composition-receipt.json")
    )
    allowed.update(
        (pathlib.Path("examples/diagnostic-envelope") / path.name).as_posix()
        for path in (root / "drafts/diagnostic-envelope/fixtures").glob("*.json")
    )
    for plan_name, expected_parent in (
        ("reports/data-go-kr/operation-materialization-plan.json", "reports/data-go-kr/operation-materialization-batches"),
        ("reports/data-go-kr/institution-runtime-plan.json", "reports/data-go-kr/institution-batches"),
    ):
        plan_path = root / plan_name
        if not plan_path.is_file():
            continue
        plan = load_object(plan_path)
        batches = plan.get("batches", [])
        if not isinstance(batches, list):
            raise PromotionError(f"generated batch plan is malformed: {plan_name}")
        for batch in batches:
            if not isinstance(batch, dict) or not isinstance(batch.get("output"), str):
                raise PromotionError(f"generated batch path is malformed: {plan_name}")
            output = pathlib.PurePosixPath(batch["output"])
            if (
                output.parent.as_posix() != expected_parent
                or not re.fullmatch(r"institution-[0-9]{2}\.json", output.name)
            ):
                raise PromotionError(f"generated batch path is outside its declared output inventory: {plan_name}")
            allowed.add(output.as_posix())
    return allowed


def changed_repository_paths(root: pathlib.Path) -> list[str]:
    tracked = command(("git", "diff", "--name-only", "-z", "HEAD"), root).stdout
    untracked = command(("git", "ls-files", "--others", "--exclude-standard", "-z"), root).stdout
    return sorted({path for path in (*tracked.split("\0"), *untracked.split("\0")) if path})


def stage_candidate_outputs(root: pathlib.Path, generation_id: str) -> list[str]:
    changed = changed_repository_paths(root)
    allowed = candidate_generated_file_allowlist(root, generation_id)
    unexpected = sorted(set(changed) - allowed)
    if unexpected:
        raise PromotionError("candidate generation modified files outside its explicit output allowlist: " + ", ".join(unexpected))
    if "data/data-go-kr.registry.json" not in changed or "manifest.json" not in changed:
        raise PromotionError("candidate generation did not update the canonical payload and release manifest")
    if not changed:
        raise PromotionError("candidate generation produced no source changes to stage")
    command(("git", "add", "--", *changed), root)
    staged_raw = command(("git", "diff", "--cached", "--name-only", "-z", "HEAD"), root).stdout
    unstaged_raw = command(("git", "diff", "--name-only", "-z"), root).stdout
    untracked_raw = command(("git", "ls-files", "--others", "--exclude-standard", "-z"), root).stdout
    staged = {path for path in staged_raw.split("\0") if path}
    if staged != set(changed) or unstaged_raw.strip("\0") or untracked_raw.strip("\0"):
        raise PromotionError("explicit candidate staging did not capture exactly the reviewed generated file set")
    return changed


def update_registry_review_artifacts(root: pathlib.Path, generation_id: str, bundle_dir: pathlib.Path) -> pathlib.Path:
    target = root / REVIEW_DIR / generation_id
    target.mkdir(parents=True, exist_ok=True)
    if target.is_symlink() or not target.is_dir():
        raise PromotionError("generation review artifact directory is not a regular owned directory")
    allowed = {"semantic-diff.json", "regeneration-queue.json", "quarantine.json", "composition-receipt.json"}
    for child in target.iterdir():
        if child.name not in allowed or child.is_symlink() or not child.is_file():
            raise PromotionError("generation review artifact directory contains an unexpected entry")
    for name in ("semantic-diff.json", "regeneration-queue.json", "quarantine.json", "composition-receipt.json"):
        (target / name).write_bytes((bundle_dir / name).read_bytes())
    return target


def validate_generation_identity(checkpoint: Mapping[str, Any]) -> None:
    generation_id = checkpoint.get("generation_id")
    if not isinstance(generation_id, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", generation_id):
        raise PromotionError("processor generation id is not a safe bounded path/marker identity")


def validate_no_candidate_processor_result(
    bundle_dir: pathlib.Path,
    *,
    repository: str,
    workflow_run_id: str,
    workflow_run_attempt: str,
) -> dict[str, Any] | None:
    """Recognize only a current-run idle/replay result with no candidate bundle."""
    if repository.lower() != "statpan/datapan-registry":
        raise PromotionError("idle processor result repository is outside the admitted source")
    result_path = bundle_dir / "upstream-catalogue-processing-result.json"
    if not result_path.is_file() or result_path.is_symlink():
        return None
    result = load_object(result_path)
    if result.get("status") != "idle":
        return None
    children = list(bundle_dir.iterdir())
    if len(children) != 1 or children[0] != result_path:
        raise PromotionError("idle processor result must be the only file in its immutable run artifact")
    if "generation_id" in result:
        raise PromotionError("idle processor result unexpectedly identifies a candidate generation")
    processor_run_id = f"{workflow_run_id}-{workflow_run_attempt}"
    if result.get("reason") == "exact_producer_delivery_replay":
        expected_keys = {
            "status", "reason", "processing_replay", "candidate_available", "source_id",
            "producer_run_id", "processor_run_id", "processor_artifact_run_id",
        }
        if set(result) != expected_keys:
            raise PromotionError("producer replay result fields differ from the frozen #657 contract")
        if (
            result.get("processing_replay") is not True
            or result.get("candidate_available") is not False
            or result.get("source_id") != "data_go_kr"
            or result.get("processor_run_id") != processor_run_id
            or result.get("processor_artifact_run_id") != workflow_run_id
            or not re.fullmatch(r"[0-9]{6,20}", str(result.get("producer_run_id", "")))
        ):
            raise PromotionError("producer replay result does not bind the exact processor/source identity")
    elif result.get("reason") == "no_active_generation":
        if set(result) != {"status", "reason"}:
            raise PromotionError("empty-queue idle result contains unexpected candidate identity")
    else:
        raise PromotionError("idle processor result reason is not an admitted no-candidate state")
    return {
        "status": "no-candidate",
        "reason": result["reason"],
        "source_id": result.get("source_id", "data_go_kr"),
        "processor_run_id": processor_run_id,
        "candidate_available": False,
    }


def manual_review_status(root: pathlib.Path) -> str:
    report = load_object(root / "reports/credential-runtime-manual-review-acceptance.json")
    summary = report.get("summary")
    if not isinstance(summary, dict):
        raise PromotionError("manual-review acceptance report has no summary object")
    status = summary.get("acceptance_status")
    if status not in {"accepted", "revalidation_required", "unproven"}:
        raise PromotionError("manual-review acceptance report is malformed or has an unsupported status")
    if summary.get("accepted") is not (status == "accepted"):
        raise PromotionError("manual-review acceptance summary does not agree with its current effective status")
    return str(status)


def finish_review_policy_status(root: pathlib.Path) -> str:
    """Return whether this repository explicitly configures a supported Gira policy.

    Gira accepts only ``required`` and ``none``. ``none`` still means a policy
    was configured: it explicitly waives GitHub APPROVED evidence while leaving
    independent Astra review and CI requirements in force. Require one unique
    top-level key so nested ``profiles.*.review_policy`` metadata, duplicate
    YAML keys, malformed documents, and unsupported values fail closed.
    """
    try:
        text = (root / ".gira/config.yaml").read_text(encoding="utf-8")
        document = yaml.compose(text, Loader=yaml.SafeLoader)
        if not isinstance(document, yaml.MappingNode):
            return "unconfigured"
        keys: set[str] = set()
        for key_node, _value_node in document.value:
            if not isinstance(key_node, yaml.ScalarNode) or key_node.tag != "tag:yaml.org,2002:str":
                return "unconfigured"
            key = key_node.value
            if key in keys:
                return "unconfigured"
            keys.add(key)
        if "finish_review_policy" not in keys:
            return "unconfigured"
        config = yaml.safe_load(text)
    except (OSError, UnicodeError, yaml.YAMLError, TypeError, ValueError):
        return "unconfigured"
    if not isinstance(config, dict):
        return "unconfigured"
    policy = config.get("finish_review_policy")
    if not isinstance(policy, str) or policy.strip().casefold() not in {"none", "required"}:
        return "unconfigured"
    return "configured"


def gh_json(root: pathlib.Path, *arguments: str) -> Any:
    result = command(("gh", *arguments), root)
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise PromotionError("GitHub CLI returned invalid JSON") from exc


def repository_owner_id(helper: Any, repository: str, source_id: str, scope: str) -> str:
    return helper.owner_id(repository, source_id, scope)


def issue_marker(owner_id: str, generation_id: str) -> str:
    return f"<!-- {PROMOTION_ISSUE_MARKER}{owner_id.removeprefix('datapan-canonical-update:v1:')} generation={generation_id} -->"


def ensure_candidate_issue(
    root: pathlib.Path,
    repository: str,
    candidate: Mapping[str, Any],
    owner: str,
    existing_issue_number: int = 0,
) -> tuple[int, str]:
    """Reuse an issue only when its durable ownership marker matches exactly."""
    marker = issue_marker(owner, str(candidate["generation_id"]))
    issues = gh_json(root, "issue", "list", "--repo", repository, "--state", "all", "--limit", "1000", "--json", "number,title,body,url,state,labels")
    matches = [row for row in issues if isinstance(row.get("body"), str) and marker in row["body"]]
    open_matches = [row for row in matches if str(row.get("state", "")).upper() == "OPEN"]
    if len(open_matches) > 1:
        raise PromotionError("duplicate_candidate_issues: preserve all issues and resolve ownership before retry")
    if existing_issue_number:
        existing = gh_json(root, "issue", "view", str(existing_issue_number), "--repo", repository, "--json", "number,title,body,url,state")
        stable_prefix = f"<!-- {PROMOTION_ISSUE_MARKER}{owner.removeprefix('datapan-canonical-update:v1:')} "
        if existing.get("state") != "OPEN" or not isinstance(existing.get("body"), str) or not existing["body"].startswith(stable_prefix):
            raise PromotionError("durable candidate issue is closed or no longer owned; preserve its history")
        if open_matches and int(open_matches[0].get("number", 0)) != existing_issue_number:
            raise PromotionError("candidate issue differs from the durable open-PR ownership receipt")
        if marker not in existing["body"]:
            body_path = root / ".datapan/candidate-issue.md"
            body_path.parent.mkdir(parents=True, exist_ok=True)
            body_path.write_text("\n".join((
                marker, "", "Parent work: #655", "",
                "This bounded automation issue tracks one owned canonical registry update PR. Review the exact candidate source, scoped composition, generated release evidence, and repository validation before merge. This issue does not authorize merging or Hugging Face publication.", "",
                f"Source: `{candidate['source_id']}`", f"Scope: `{candidate['scope']}`", f"Generation: `{candidate['generation_id']}`", "",
            )), encoding="utf-8")
            command(("gh", "issue", "edit", str(existing_issue_number), "--repo", repository,
                     "--title", f"Review canonical registry update: {candidate['source_id']} ({candidate['generation_id']})",
                     "--body-file", str(body_path)), root)
        return existing_issue_number, str(existing.get("url", ""))
    if open_matches:
        issue = open_matches[0]
        return int(issue["number"]), str(issue.get("url", ""))

    body = "\n".join((
        marker,
        "",
        "Parent work: #655",
        "",
        "This bounded automation issue tracks one owned canonical registry update PR. Review the exact candidate source, scoped composition, generated release evidence, and repository validation before merge. This issue does not authorize merging or Hugging Face publication.",
        "",
        f"Source: `{candidate['source_id']}`",
        f"Scope: `{candidate['scope']}`",
        f"Generation: `{candidate['generation_id']}`",
    ))
    body_path = root / ".datapan/candidate-issue.md"
    body_path.parent.mkdir(parents=True, exist_ok=True)
    body_path.write_text(body + "\n", encoding="utf-8")
    title = f"Review canonical registry update: {candidate['source_id']} ({candidate['generation_id']})"
    output = command((
        "gh", "issue", "create", "--repo", repository, "--title", title,
        "--body-file", str(body_path), "--label", "type:task", "--label", "status:ready", "--label", "priority:p1",
    ), root)
    url = output.stdout.strip().splitlines()[-1] if output.stdout.strip() else ""
    match = re.search(r"/issues/(\d+)$", url)
    if not match:
        # A create response can be lost after the server committed. Resolve by
        # the stable marker before any retry can create another issue.
        reread = gh_json(root, "issue", "list", "--repo", repository, "--state", "all", "--limit", "1000", "--json", "number,title,body,url,state,labels")
        recovered = [
            row for row in reread
            if isinstance(row.get("body"), str)
            and marker in row["body"]
            and str(row.get("state", "")).upper() == "OPEN"
        ]
        if len(recovered) != 1 or recovered[0].get("state") != "OPEN":
            raise PromotionError("candidate issue creation lacks an authoritative marker read-back")
        return int(recovered[0]["number"]), str(recovered[0].get("url", ""))
    return int(match.group(1)), url


def gh_open_prs(root: pathlib.Path, repository: str) -> list[dict[str, Any]]:
    values = gh_json(root, "pr", "list", "--repo", repository, "--state", "all", "--limit", "1000", "--json", "number,url,state,body,headRefName,headRefOid,baseRefName,mergeCommit")
    rows: list[dict[str, Any]] = []
    for value in values:
        if not isinstance(value, dict):
            continue
        body = value.get("body") if isinstance(value.get("body"), str) else ""
        owner_match = re.search(r"<!-- (datapan-canonical-update:v1:[a-f0-9]{64}) generation=([^ ]+) -->", body)
        rows.append({
            "number": int(value.get("number", 0)),
            "url": str(value.get("url", "")),
            "state": str(value.get("state", "")).lower(),
            "body": body,
            "body_sha256": hashlib.sha256(body.encode("utf-8")).hexdigest(),
            "head_sha": value.get("headRefOid"),
            "head_ref": value.get("headRefName"),
            "base_ref": value.get("baseRefName"),
            "merge_commit_sha": (value.get("mergeCommit") or {}).get("oid") if isinstance(value.get("mergeCommit"), dict) else None,
            "owner_id": owner_match.group(1) if owner_match else "",
            "generation_id": owner_match.group(2) if owner_match else "",
        })
    return rows


def gh_pr_readback(root: pathlib.Path, repository: str, number: int) -> dict[str, Any]:
    value = gh_json(root, "pr", "view", str(number), "--repo", repository, "--json", "number,url,state,body,headRefName,headRefOid,baseRefName,mergeCommit")
    pull = gh_rest_json(root, f"repos/{repository}/pulls/{number}", "--method", "GET")
    base = pull.get("base") if isinstance(pull, Mapping) else None
    head = pull.get("head") if isinstance(pull, Mapping) else None
    authoritative_base_sha = base.get("sha") if isinstance(base, Mapping) else None
    base_repository = base.get("repo") if isinstance(base, Mapping) else None
    head_repository = head.get("repo") if isinstance(head, Mapping) else None
    authoritative_base = base_repository.get("full_name") if isinstance(base_repository, Mapping) else None
    authoritative_head = head_repository.get("full_name") if isinstance(head_repository, Mapping) else None
    return {
        "number": value.get("number"), "url": value.get("url"), "state": value.get("state"),
        "body": value.get("body"), "headRefName": value.get("headRefName"),
        "headRefOid": value.get("headRefOid"), "baseRefName": value.get("baseRefName"),
        "baseRefOid": authoritative_base_sha,
        "mergeCommit": value.get("mergeCommit"),
        "repository": authoritative_base,
        "headRepository": authoritative_head,
        "merged": pull.get("merged") if isinstance(pull, Mapping) else None,
        "mergedAt": pull.get("merged_at") if isinstance(pull, Mapping) else None,
    }


def promotion_journal_worktree(root: pathlib.Path, path: pathlib.Path, source_base_sha: str) -> tuple[str | None, bool]:
    """Create an isolated worktree at the exact currently observed journal ref."""
    module = load_module(root / "scripts/materialize-canonical-registry.py", "journal_git_helper")
    ref = f"refs/heads/{STATE_BRANCH}"
    current = pr_helper_remote_sha(module, root, ref)
    if current:
        try:
            module.git_output(["fetch", "--no-tags", "origin", f"+{ref}:refs/remotes/origin/canonical-update-state"], root, availability=True)
        except module.AvailabilityError as exc:
            raise PromotionError("promotion state branch could not be fetched for compare-and-swap") from exc
        base = "refs/remotes/origin/canonical-update-state"
    else:
        base = source_base_sha
    result = subprocess.run(("git", "worktree", "add", "--detach", str(path), base), cwd=root, text=True, capture_output=True, check=False)
    if result.returncode != 0:
        raise PromotionError("promotion state worktree could not be created from the observed state branch")
    return current, current is not None


def pr_helper_remote_sha(module: Any, root: pathlib.Path, ref: str) -> str | None:
    try:
        result = module.git_output(["ls-remote", "--heads", "origin", ref], root, availability=True)
    except module.AvailabilityError as exc:
        raise PromotionError("promotion state branch read failed") from exc
    matches = [line.split("\t", 1)[0] for line in result.decode("ascii", errors="replace").splitlines() if line.endswith("\t" + ref)]
    if len(matches) > 1:
        raise PromotionError("promotion state branch returned duplicate refs")
    return matches[0] if matches else None


def assert_remote_main_sha(root: pathlib.Path, expected_sha: str) -> None:
    if not re.fullmatch(r"[a-f0-9]{40}", expected_sha):
        raise PromotionError("trusted source refresh target main is not a full immutable Git SHA")
    result = command(("git", "ls-remote", "--heads", "origin", "refs/heads/main"), root)
    rows = [line.split("\t", 1)[0] for line in result.stdout.splitlines() if line.endswith("\trefs/heads/main")]
    if rows != [expected_sha]:
        raise PromotionError("trusted source refresh target main moved; preserve the owned PR and re-read")


def assert_predecessor_base_is_ancestor(
    root: pathlib.Path, predecessor_base_sha: str, target_main_sha: str,
) -> None:
    if not re.fullmatch(r"[a-f0-9]{40}", predecessor_base_sha):
        raise PromotionError("source refresh predecessor base is not a full immutable Git SHA")
    if not re.fullmatch(r"[a-f0-9]{40}", target_main_sha):
        raise PromotionError("source refresh target main is not a full immutable Git SHA")
    result = command(
        ("git", "merge-base", "--is-ancestor", predecessor_base_sha, target_main_sha),
        root, allowed_returncodes=frozenset({0, 1}),
    )
    if result.returncode != 0:
        raise PromotionError("source refresh target main is not a descendant of the predecessor candidate base")


def persist_journal_record(
    root: pathlib.Path,
    source_base_sha: str,
    receipt: Mapping[str, Any],
    *,
    observed_at: str,
    expected_ci: Any = CI_EXPECTATION_UNSET,
    supersede_from: Mapping[str, Any] | None = None,
    expected_state_sha: Any = STATE_EXPECTATION_UNSET,
) -> str | None:
    helper = load_module(root / "scripts/canonical_update_pr.py", "canonical_update_journal_helper")
    schema = load_object(root / "schemas/datapan.canonical-update-promotion-journal.v1.schema.json")
    path = root / ".datapan/promotion-state-worktree"
    if path.exists():
        subprocess.run(("git", "worktree", "remove", "--force", str(path)), cwd=root, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
        shutil.rmtree(path, ignore_errors=True)
    old_sha, _ = promotion_journal_worktree(root, path, source_base_sha)
    try:
        if expected_state_sha is not STATE_EXPECTATION_UNSET and old_sha != expected_state_sha:
            raise PromotionError("promotion state compare-and-swap conflict: durable journal changed during refresh")
        journal_path = path / JOURNAL_PATH
        journal = None
        if journal_path.is_file():
            journal = load_object(journal_path)
            helper.validate_journal(journal, schema)
        if expected_ci is not CI_EXPECTATION_UNSET:
            try:
                helper.assert_ci_compare_and_swap(journal, receipt, expected_ci)
            except helper.AdmissionError as exc:
                raise PromotionError(str(exc)) from exc
        updated = helper.append_journal_record(
            journal, receipt, repository=str(receipt["candidate"]["repository"]),
            observed_at=observed_at, supersede_from=supersede_from,
        )
        helper.validate_journal(updated, schema)
        journal_path.parent.mkdir(parents=True, exist_ok=True)
        journal_path.write_bytes(json.dumps(updated, ensure_ascii=False, indent=2).encode("utf-8") + b"\n")
        command(("git", "-C", str(path), "add", "--", JOURNAL_PATH.as_posix()), root)
        staged = command(("git", "-C", str(path), "diff", "--cached", "--quiet"), root, allowed_returncodes=frozenset({0, 1}))
        if staged.returncode == 0:
            return old_sha
        command(("git", "-C", str(path), "-c", "user.name=datapan-canonical-update[bot]", "-c", "user.email=41898282+github-actions[bot]@users.noreply.github.com", "commit", "-m", "Record canonical update promotion acknowledgement"), root)
        new_sha = command(("git", "-C", str(path), "rev-parse", "HEAD"), root).stdout.strip()
        ref = f"refs/heads/{STATE_BRANCH}"
        lease = f"--force-with-lease={ref}:{old_sha or ''}"
        command(("git", "-C", str(path), "push", "--no-verify", lease, "origin", f"{new_sha}:{ref}"), root)
        observed = pr_helper_remote_sha(helper.load_materializer(root), root, ref)
        if observed != new_sha:
            raise PromotionError("promotion state branch read-back differs from the exact journal commit")
        return new_sha
    finally:
        subprocess.run(("git", "worktree", "remove", "--force", str(path)), cwd=root, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)


def load_promotion_journal_snapshot(root: pathlib.Path) -> tuple[dict[str, Any] | None, str | None]:
    helper = load_module(root / "scripts/canonical_update_pr.py", "canonical_update_journal_reader")
    module = helper.load_materializer(root)
    ref = f"refs/heads/{STATE_BRANCH}"
    sha = pr_helper_remote_sha(module, root, ref)
    if not sha:
        return None, None
    try:
        module.git_output(["fetch", "--no-tags", "origin", f"+{ref}:refs/remotes/origin/canonical-update-state"], root, availability=True)
    except module.AvailabilityError as exc:
        raise PromotionError("promotion state branch could not be fetched") from exc
    try:
        raw = module.git_output(["show", f"{sha}:{JOURNAL_PATH.as_posix()}"], root)
    except module.IntegrityError:
        return None, sha
    try:
        journal = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise PromotionError("promotion state journal is malformed JSON") from exc
    helper.validate_journal(journal, load_object(root / "schemas/datapan.canonical-update-promotion-journal.v1.schema.json"))
    return journal, sha


def load_promotion_journal(root: pathlib.Path) -> dict[str, Any] | None:
    return load_promotion_journal_snapshot(root)[0]


def ensure_verify_release_ci(root: pathlib.Path, receipt: Mapping[str, Any]) -> dict[str, Any]:
    """Reconcile or dispatch the exact owned PR head and persist every CI step to its journal row."""
    module = load_module(root / "scripts/canonical_update_ci.py", "canonical_update_ci")
    ownership_helper = load_module(root / "scripts/canonical_update_pr.py", "canonical_update_pr_ci_ownership")
    repository = str(receipt.get("candidate", {}).get("repository", ""))
    candidate = receipt.get("candidate", {})
    source_id = str(candidate.get("source_id", ""))
    scope = str(candidate.get("scope", ""))
    generation_id = str(candidate.get("generation_id", ""))
    registry_sha256 = str(candidate.get("registry_sha256", ""))
    candidate_head_sha = str(candidate.get("head_sha", ""))
    current_journal = load_promotion_journal(root)
    current = journal_record_for(
        current_journal, source_id, scope, generation_id, registry_sha256,
        candidate_head_sha=candidate_head_sha,
    )
    if current is None:
        raise PromotionError("owned PR has no durable promotion receipt before verify-release CI")
    if current.get("superseded_by") is not None:
        raise PromotionError("verify-release CI cannot gate a superseded candidate revision")
    if current.get("status") not in {"prepared", "pending-review"} or current.get("pr", {}).get("number", 0) < 1:
        raise PromotionError("verify-release CI is limited to a durably owned active candidate PR")
    expected_ci = json.loads(json.dumps(current.get("ci"))) if current.get("ci") is not None else None
    source_base_sha = command(("git", "rev-parse", "HEAD"), root).stdout.strip()

    def persist_ci(entry: dict[str, Any]) -> None:
        nonlocal expected_ci
        latest_journal = load_promotion_journal(root)
        latest = journal_record_for(
            latest_journal, source_id, scope, generation_id, registry_sha256,
            candidate_head_sha=candidate_head_sha,
        )
        if latest is None:
            raise PromotionError("verify-release CI candidate disappeared from the durable state branch")
        updated = json.loads(json.dumps(latest))
        updated["ci"] = json.loads(json.dumps(entry))
        persist_journal_record(
            root, source_base_sha, updated,
            observed_at=dt.datetime.now(dt.timezone.utc).isoformat(),
            expected_ci=expected_ci,
        )
        expected_ci = json.loads(json.dumps(entry))

    def dispatch(branch: str, inputs: Mapping[str, str]) -> tuple[int | None, Mapping[str, Any] | None]:
        workflow = urllib.parse.quote(VERIFY_WORKFLOW_PATH, safe="")
        status, body = github_api_request("POST", f"repos/{repository}/actions/workflows/{workflow}/dispatches", {
            "ref": branch,
            "inputs": dict(inputs),
        })
        return status, body if isinstance(body, Mapping) else None

    result = module.ensure_verify_release_run(
        repository,
        current,
        lambda: gh_pr_readback(root, repository, int(current["pr"]["number"])),
        expected_ci,
        read_branch_sha=lambda branch: verify_release_branch_sha(repository, branch),
        list_matching_runs=list_verify_release_runs,
        dispatch=dispatch,
        read_run=lambda run_id: read_verify_release_run(repository, run_id),
        persist=persist_ci,
        branch_matches=ownership_helper.automation_branch_matches,
    )
    return result


def active_owned_pr(journal: Mapping[str, Any] | None, github_prs: Sequence[Mapping[str, Any]], helper: Any, candidate: Mapping[str, Any]) -> tuple[list[dict[str, Any]], int]:
    if not isinstance(journal, Mapping):
        related_github = [row for row in github_prs if row.get("owner_id") == helper.owner_id(candidate["repository"], candidate["source_id"], candidate["scope"]) and row.get("state") == "open"]
        if related_github:
            raise PromotionError("open_candidate_pr_without_durable_owner_receipt: preserve it and investigate state branch")
        return [], 0
    owner = helper.owner_id(candidate["repository"], candidate["source_id"], candidate["scope"])
    records = [
        row for row in journal.get("records", [])
        if row.get("ownership", {}).get("owner_id") == owner
        and row.get("superseded_by") is None
        and row.get("status") in {"prepared", "pending-review"}
        and row.get("pr", {}).get("number", 0) > 0
    ]
    active = []
    for record in records:
        number = int(record["pr"]["number"])
        matches = [row for row in github_prs if int(row.get("number", 0)) == number]
        if len(matches) != 1:
            raise PromotionError("durable candidate PR is not present in authoritative GitHub read-back")
        observed = matches[0]
        if observed.get("state") != "open":
            continue
        expected_head = record["candidate"]["head_sha"]
        expected_body = record["ownership"]["body_sha256"]
        if observed.get("head_sha") != expected_head:
            raise PromotionError("human_head_change: preserve the owned PR branch and stop")
        if observed.get("body_sha256") != expected_body:
            raise PromotionError("human_body_change: preserve the owned PR body and stop")
        if observed.get("owner_id") != owner:
            raise PromotionError("PR owner marker differs from durable promotion journal")
        active.append({
            "repository": candidate["repository"], "source_id": candidate["source_id"], "scope": candidate["scope"],
            "state": "open", "owner_id": owner, "number": number,
            "head_sha": observed["head_sha"], "automation_head_sha": expected_head,
            "body_sha256": observed["body_sha256"], "automation_body_sha256": expected_body,
            "candidate_head_sha": record["candidate"]["head_sha"],
            "manifest_sha256": record["candidate"]["manifest_sha256"],
            "registry_sha256": record["candidate"]["registry_sha256"],
        })
    if len(active) > 1:
        raise PromotionError("duplicate_open_prs: preserve all candidate branches and resolve ownership")
    issue = int(active[0].get("issue_number", 0)) if active else 0
    if active:
        source_record = next(row for row in records if int(row["pr"]["number"]) == active[0]["number"])
        issue = int(source_record.get("ownership", {}).get("issue_number", 0))
    return active, issue


def run_bound_validation(root: pathlib.Path, datapan_cli: pathlib.Path, candidate: Mapping[str, Any]) -> list[dict[str, Any]]:
    commands_by_check = {
        "registry_manifest": [
            ("python3", "scripts/sync-release-schema-artifacts.py", "--check"),
            ("python3", "scripts/sync-release-manifest-artifacts.py", "--check"),
        ],
        "denominator": [
            ("python3", "scripts/validate-data-go-kr-operation-manifest.py"),
            ("python3", "scripts/generate-operation-denominator-rollup.py", "--check"),
        ],
        "adapter": [
            ("python3", "scripts/validate-external-adapter-backlog.py"),
            ("python3", "scripts/validate-source-profiles.py"),
        ],
        "ledger": [("python3", "scripts/refresh-release-ledger-evidence.py", "--check")],
        "diagnostic_current_source_applicability": [
            ("python3", "scripts/generate-diagnostic-current-source-applicability.py", "--check"),
            ("python3", "scripts/validate-diagnostic-current-source-applicability.py"),
        ],
        "release": [
            ("python3", "scripts/validate-release-report-artifacts.py"),
            ("python3", "scripts/validate-release-receipt-boundary.py"),
            ("python3", "scripts/generate-diagnostic-publication.py", "--check"),
        ],
        "consumer": [("python3", "scripts/validate-release-consumer-compatibility.py")],
    }
    evidence: list[dict[str, Any]] = [{
        "name": "composition", "source_sha": candidate["head_sha"], "manifest_sha256": candidate["manifest_sha256"],
        "command": "#656 receipt schema + exact bundle digest/arithmetic validation", "exit_code": 0,
    }]
    for name, commands in commands_by_check.items():
        for argv in commands:
            completed = command(argv, root)
            evidence.append({
                "name": name, "source_sha": candidate["head_sha"], "manifest_sha256": candidate["manifest_sha256"],
                "command": shlex.join(argv), "exit_code": completed.returncode,
            })
    for argv in (
        ("python3", "scripts/validate-credential-runtime-manual-review-decision.py"),
        ("python3", "scripts/generate-credential-runtime-manual-review-acceptance.py", "--check"),
    ):
        completed = command(argv, root)
        evidence.append({
            "name": "manual_review_acceptance", "source_sha": candidate["head_sha"],
            "manifest_sha256": candidate["manifest_sha256"], "command": shlex.join(argv),
            "exit_code": completed.returncode,
        })
    return evidence


def refresh_pr_phase(
    target: Mapping[str, Any],
    predecessor: Mapping[str, Any],
    observed: Mapping[str, Any],
    helper: Any,
    *,
    require_repository: bool = True,
) -> str:
    """Classify only the three durable states of an owned PR refresh."""
    target_candidate = target.get("candidate", {})
    target_owner = target.get("ownership", {})
    target_pr = target.get("pr", {})
    old_candidate = predecessor.get("candidate", {})
    old_owner = predecessor.get("ownership", {})
    old_pr = predecessor.get("pr", {})
    if not helper._reference_matches_receipt(target.get("refresh_from", {}), predecessor):
        raise PromotionError("prepared refresh intent does not bind its exact predecessor head/body")
    target_main = target.get("refresh_target_main_sha")
    if target_main is not None and (
        not isinstance(target_main, str)
        or not re.fullmatch(r"[a-f0-9]{40}", target_main)
        or target_candidate.get("base_sha") != target_main
    ):
        raise PromotionError("prepared source refresh intent does not bind the exact trusted target main")
    if (
        target_owner.get("owner_id") != old_owner.get("owner_id")
        or target_owner.get("branch") != old_owner.get("branch")
        or target_owner.get("issue_number") != old_owner.get("issue_number")
        or target_pr.get("number") != old_pr.get("number")
        or target_pr.get("number", 0) < 1
    ):
        raise PromotionError("prepared refresh intent changed the owned issue, branch, or PR")
    if require_repository and (
        str(observed.get("repository", "")).casefold() != str(target_candidate.get("repository", "")).casefold()
        or str(observed.get("headRepository", "")).casefold() != str(target_candidate.get("repository", "")).casefold()
    ):
        raise PromotionError("owned PR base/head repository read-back differs from the candidate repository")
    if (
        str(observed.get("state", "")).upper() != "OPEN"
        or int(observed.get("number", 0)) != int(target_pr.get("number", 0))
        or observed.get("headRefName") != target_owner.get("branch")
        or observed.get("baseRefName") != "main"
    ):
        raise PromotionError("owned PR branch/base/number changed during candidate refresh")
    body = observed.get("body")
    if not isinstance(body, str):
        raise PromotionError("owned PR refresh read-back has no body")
    body_sha = hashlib.sha256(body.encode("utf-8")).hexdigest()
    claimed_body_sha = observed.get("body_sha256")
    if claimed_body_sha is not None and claimed_body_sha != body_sha:
        raise PromotionError("owned PR body digest differs from its API read-back")
    target_body = target_owner.get("body")
    if not isinstance(target_body, str) or hashlib.sha256(target_body.encode("utf-8")).hexdigest() != target_owner.get("body_sha256"):
        raise PromotionError("prepared refresh intent lacks its exact durable target body")
    owner = str(target_owner.get("owner_id", ""))
    old_marker = helper.body_marker(owner, str(old_candidate.get("generation_id", "")))
    new_marker = helper.body_marker(owner, str(target_candidate.get("generation_id", "")))
    if old_marker not in body and new_marker not in body:
        raise PromotionError("owned PR body no longer has either exact refresh owner marker")
    observed_head = str(observed.get("headRefOid", ""))
    old_head = str(old_candidate.get("head_sha", ""))
    target_head = str(target_candidate.get("head_sha", ""))
    old_body_sha = str(old_owner.get("body_sha256", ""))
    new_body_sha = str(target_owner.get("body_sha256", ""))
    if observed_head == old_head and body_sha == old_body_sha and old_marker in body:
        return "before-push"
    if observed_head == target_head and body_sha == old_body_sha and old_marker in body:
        return "after-push-before-body"
    if observed_head == target_head and body_sha == new_body_sha and new_marker in body:
        return "after-body-edit"
    raise PromotionError("human_head_change_or_body_change: preserve the owned PR and stop refresh recovery")


def complete_prepared_refresh(
    root: pathlib.Path,
    repository: str,
    intent: Mapping[str, Any],
    predecessor: Mapping[str, Any],
    helper: Any,
    observed: Mapping[str, Any],
    *,
    controller_head_sha: str,
    journal_source_base_sha: str,
    expected_state_sha: Any,
    observed_at: str,
    run_url: str,
) -> dict[str, Any]:
    """Finish only an already-pushed exact refresh under the current trusted controller head."""
    candidate = intent.get("candidate", {})
    old_candidate = predecessor.get("candidate", {})
    owner = intent.get("ownership", {})
    old_owner = predecessor.get("ownership", {})
    explicit_source_refresh = isinstance(intent.get("refresh_target_main_sha"), str)
    predecessor_statuses = {"pending-review"} if explicit_source_refresh else {"pending-review", "prepared"}
    if (
        intent.get("status") != "prepared"
        or intent.get("superseded_by") is not None
        or predecessor.get("status") not in predecessor_statuses
        or predecessor.get("superseded_by") is not None
        or not helper._reference_matches_receipt(intent.get("refresh_from", {}), predecessor)
        or candidate.get("repository") != old_candidate.get("repository")
        or candidate.get("source_id") != old_candidate.get("source_id")
        or candidate.get("scope") != old_candidate.get("scope")
        or (
            explicit_source_refresh
            and (
                candidate.get("generation_id") != old_candidate.get("generation_id")
                or candidate.get("registry_sha256") != old_candidate.get("registry_sha256")
                or candidate.get("composition_receipt_sha256") != old_candidate.get("composition_receipt_sha256")
            )
        )
        or owner.get("owner_id") != old_owner.get("owner_id")
        or owner.get("branch") != old_owner.get("branch")
        or owner.get("issue_number") != old_owner.get("issue_number")
        or intent.get("pr", {}).get("number") != predecessor.get("pr", {}).get("number")
    ):
        raise PromotionError("prepared refresh transaction changed its exact predecessor, B payload, issue, branch, or PR")
    phase = refresh_pr_phase(intent, predecessor, observed, helper)
    if phase == "before-push":
        raise PromotionError("prepared refresh has not pushed its exact successor head")
    number = int(intent.get("pr", {}).get("number", 0))
    body = owner.get("body")
    if not isinstance(body, str) or not body:
        raise PromotionError("prepared refresh intent has no exact target body")
    if phase == "after-push-before-body":
        # The persisted exact head is already on the owned PR. Finish only its
        # matching body under the currently checked-out trusted controller.
        assert_remote_main_sha(root, controller_head_sha)
        body_path = root / ".datapan/candidate-pr.md"
        body_path.parent.mkdir(parents=True, exist_ok=True)
        body_path.write_text(body, encoding="utf-8")
        try:
            command((
                "gh", "pr", "edit", str(number), "--repo", repository,
                "--title", pr_title(candidate), "--body-file", str(body_path),
            ), root)
        except (OSError, subprocess.SubprocessError, PromotionError):
            # GitHub may have accepted the body edit before the local command
            # reported failure. Only a fresh exact API read-back can decide.
            pass
        observed = gh_pr_readback(root, repository, number)
        phase = refresh_pr_phase(intent, predecessor, observed, helper)
        if phase != "after-body-edit":
            raise PromotionError("refresh body edit did not read back the exact target head and body")
    elif phase != "after-body-edit":
        raise PromotionError("prepared refresh is not at an exact recoverable PR phase")

    # Main may have advanced beyond the immutable target after the candidate
    # branch was pushed. The current workflow must still be executing from the
    # latest trusted main, while the durable intent authorizes only this exact
    # already-applied head/body transaction.
    assert_remote_main_sha(root, controller_head_sha)
    acknowledged = json.loads(json.dumps(intent))
    acknowledged["ownership"]["expected_head_sha"] = candidate["head_sha"]
    try:
        refreshed = helper.record_pr_readback(
            acknowledged, observed, observed_at=observed_at, run_url=run_url,
        )
    except helper.AdmissionError as exc:
        raise PromotionError("prepared refresh read-back could not bind its exact pending-review acknowledgement") from exc
    if (
        refreshed.get("candidate") != intent.get("candidate")
        or refreshed.get("ownership", {}).get("body_sha256") != owner.get("body_sha256")
        or refreshed.get("status") != "pending-review"
        or refreshed.get("pr", {}).get("number") != number
    ):
        raise PromotionError("prepared refresh acknowledgement changed immutable candidate identity or ownership")
    persist_journal_record(
        root, journal_source_base_sha, refreshed, observed_at=observed_at,
        # Keep the immutable reference bytes that were recorded in the intent.
        # Older references may omit the optional manifest digest while still
        # resolving unambiguously to this exact predecessor revision.
        supersede_from=intent["refresh_from"],
        expected_state_sha=expected_state_sha,
    )
    return refreshed


def validate_exact_open_pr(receipt: Mapping[str, Any], observed: Mapping[str, Any], helper: Any) -> None:
    candidate = receipt.get("candidate", {})
    ownership = receipt.get("ownership", {})
    pr = receipt.get("pr", {})
    body = observed.get("body")
    if (
        str(observed.get("repository", "")).casefold() != str(candidate.get("repository", "")).casefold()
        or str(observed.get("headRepository", "")).casefold() != str(candidate.get("repository", "")).casefold()
        or str(observed.get("state", "")).upper() != "OPEN"
        or int(observed.get("number", 0)) != int(pr.get("number", 0))
        or observed.get("headRefName") != ownership.get("branch")
        or observed.get("baseRefName") != "main"
        or observed.get("headRefOid") != candidate.get("head_sha")
        or not isinstance(body, str)
        or hashlib.sha256(body.encode("utf-8")).hexdigest() != ownership.get("body_sha256")
        or (isinstance(ownership.get("body"), str) and body != ownership.get("body"))
        or helper.body_marker(str(ownership.get("owner_id", "")), str(candidate.get("generation_id", ""))) not in body
    ):
        raise PromotionError("human_head_change_or_body_change: preserve the owned PR and stop")


def validate_existing_pr_api_readback(
    observed: Mapping[str, Any],
    journal: Mapping[str, Any] | None,
    existing: Sequence[Mapping[str, Any]],
    helper: Any,
) -> None:
    """Bind an existing PR to its exact durable revision before any candidate upload."""
    if len(existing) != 1 or not isinstance(journal, Mapping):
        raise PromotionError("owned PR read-back has no unique durable revision receipt")
    reference = existing[0].get("revision_ref")
    if not isinstance(reference, Mapping):
        raise PromotionError("owned PR list result has no exact durable revision identity")
    receipt = journal_record_for(
        journal,
        str(reference.get("source_id", "")),
        str(reference.get("scope", "")),
        str(reference.get("generation_id", "")),
        str(reference.get("registry_sha256", "")),
        candidate_head_sha=str(reference.get("head_sha", "")),
    )
    if receipt is None or not helper._reference_matches_receipt(reference, receipt):
        raise PromotionError("owned PR read-back revision differs from its durable journal identity")
    if int(receipt.get("pr", {}).get("number", 0)) == 0:
        candidate = receipt.get("candidate", {})
        ownership = receipt.get("ownership", {})
        body = observed.get("body")
        if (
            str(observed.get("repository", "")).casefold() != str(candidate.get("repository", "")).casefold()
            or str(observed.get("headRepository", "")).casefold() != str(candidate.get("repository", "")).casefold()
            or str(observed.get("state", "")).upper() != "OPEN"
            or int(observed.get("number", 0)) < 1
            or observed.get("headRefName") != ownership.get("branch")
            or observed.get("baseRefName") != "main"
            or observed.get("headRefOid") != candidate.get("head_sha")
            or not isinstance(body, str)
            or hashlib.sha256(body.encode("utf-8")).hexdigest() != ownership.get("body_sha256")
            or (isinstance(ownership.get("body"), str) and body != ownership.get("body"))
            or helper.body_marker(str(ownership.get("owner_id", "")), str(candidate.get("generation_id", ""))) not in body
        ):
            raise PromotionError("interrupted PR creation read-back differs from its exact prepared owner/head/body")
        return
    if isinstance(receipt.get("refresh_from"), Mapping):
        validate_exact_open_pr(receipt, observed, helper)
        return
    intents = [
        row for row in journal.get("records", [])
        if isinstance(row, Mapping)
        and row.get("status") == "prepared"
        and row.get("superseded_by") is None
        and row.get("refresh_from") == dict(reference)
        and int(row.get("pr", {}).get("number", 0)) == int(receipt.get("pr", {}).get("number", 0))
    ]
    if len(intents) > 1:
        raise PromotionError("multiple prepared refresh intents claim one owned PR head")
    if not intents:
        validate_exact_open_pr(receipt, observed, helper)
        return
    refresh_pr_phase(intents[0], receipt, observed, helper)


def validate_prepared_create_pr_readback(
    receipt: Mapping[str, Any],
    pull_requests: Sequence[Mapping[str, Any]],
    observed: Mapping[str, Any],
    remote_branch_sha: str | None,
    helper: Any,
    *,
    repository: str,
    source_id: str,
    scope: str,
    generation_id: str,
    registry_path: str,
    registry_bytes: int,
    registry_sha256: str,
    composition_receipt_sha256: str,
) -> int:
    """Require exact evidence for an unbound create or pending-review create receipt."""
    candidate = receipt.get("candidate")
    ownership = receipt.get("ownership")
    pr_record = receipt.get("pr")
    pr_number = pr_record.get("number") if isinstance(pr_record, Mapping) else None
    prepared_pr_zero = (
        receipt.get("status") == "prepared"
        and isinstance(pr_record, Mapping)
        and not isinstance(pr_number, bool)
        and pr_number == 0
        and pr_record.get("url") == ""
        and pr_record.get("state") == "missing"
        and pr_record.get("merge_commit_sha") is None
    )
    pending_pr_positive = (
        receipt.get("status") == "pending-review"
        and isinstance(pr_record, Mapping)
        and not isinstance(pr_number, bool)
        and isinstance(pr_number, int)
        and pr_number > 0
        and pr_record.get("url") == f"https://github.com/{repository}/pull/{pr_number}"
        and pr_record.get("state") == "open"
        and (
            pr_record.get("merge_commit_sha") is None
            or (
                isinstance(pr_record.get("merge_commit_sha"), str)
                and re.fullmatch(r"[a-f0-9]{40}", pr_record["merge_commit_sha"]) is not None
            )
        )
    )
    if (
        not (prepared_pr_zero or pending_pr_positive)
        or receipt.get("action") not in {"create", "create_replacement"}
        or receipt.get("refresh_from") is not None
        or receipt.get("superseded_by") is not None
        or not isinstance(candidate, Mapping)
        or not isinstance(ownership, Mapping)
        or not isinstance(pr_record, Mapping)
    ):
        raise PromotionError("owned create recovery requires an unsuperseded standalone create in prepared/pr-zero or pending-review/pr-positive state")

    if pending_pr_positive:
        acknowledgements = receipt.get("acknowledgements")
        pending_acknowledgements = [
            item for item in acknowledgements
            if isinstance(item, Mapping) and item.get("status") == "pending-review"
        ] if isinstance(acknowledgements, list) else []
        if len(pending_acknowledgements) != 1 or not helper.exact_pending_review_witness(receipt):
            raise PromotionError("pending-review PR recovery requires one exact immutable head/manifest/artifact/run-attempt acknowledgement")

    expected_candidate = {
        "repository": repository,
        "source_id": source_id,
        "scope": scope,
        "generation_id": generation_id,
        "registry_path": registry_path,
        "registry_bytes": registry_bytes,
        "registry_sha256": registry_sha256,
        "composition_receipt_sha256": composition_receipt_sha256,
    }
    if any(candidate.get(key) != value for key, value in expected_candidate.items()):
        raise PromotionError("prepared PR recovery candidate differs from the trusted processor bundle")
    for name in ("base_sha", "head_sha"):
        if (
            not isinstance(candidate.get(name), str)
            or not re.fullmatch(r"[a-f0-9]{40}", str(candidate.get(name)))
            or candidate.get(name) == "0" * 40
        ):
            raise PromotionError(f"prepared PR recovery candidate has an invalid {name}")
    for name in ("manifest_sha256", "registry_sha256", "composition_receipt_sha256"):
        if not isinstance(candidate.get(name), str) or not re.fullmatch(r"[a-f0-9]{64}", str(candidate.get(name))):
            raise PromotionError(f"prepared PR recovery candidate has an invalid {name}")
    if isinstance(candidate.get("registry_bytes"), bool) or not isinstance(candidate.get("registry_bytes"), int) or candidate["registry_bytes"] < 1:
        raise PromotionError("prepared PR recovery candidate has an invalid registry byte count")

    try:
        expected_owner = helper.owner_id(repository, source_id, scope)
    except Exception as exc:  # noqa: BLE001 - malformed durable ownership must fail closed
        raise PromotionError("prepared PR recovery ownership identity is invalid") from exc
    owner = ownership.get("owner_id")
    branch = ownership.get("branch")
    if not helper.automation_branch_matches(candidate, str(receipt["action"]), branch):
        raise PromotionError("prepared PR recovery branch is not an exact canonical or inherited owned branch")
    expected_branch = branch
    issue_number = ownership.get("issue_number")
    expected_head = ownership.get("expected_head_sha")
    if (
        owner != expected_owner
        or branch != expected_branch
        or expected_head not in {"0" * 40, candidate.get("head_sha")}
        or isinstance(issue_number, bool)
        or not isinstance(issue_number, int)
        or issue_number < 1
        or ownership.get("issue_url") != f"https://github.com/{repository}/issues/{issue_number}"
    ):
        raise PromotionError("prepared PR recovery owner, branch, expected head, or issue identity differs")

    body = ownership.get("body")
    body_sha = ownership.get("body_sha256")
    if (
        not isinstance(body, str)
        or not isinstance(body_sha, str)
        or not re.fullmatch(r"[a-f0-9]{64}", body_sha)
        or hashlib.sha256(body.encode("utf-8")).hexdigest() != body_sha
    ):
        raise PromotionError("prepared PR recovery body bytes or digest are invalid")
    required_body_lines = (
        helper.body_marker(expected_owner, generation_id),
        f"- Source: `{source_id}`; scope: `{scope}`",
        f"- Candidate base commit: `{candidate['base_sha']}`",
        f"- Registry artifact: `{registry_path}` ({registry_bytes} bytes, sha256 `{registry_sha256}`)",
        f"- Manifest sha256: `{candidate['manifest_sha256']}`",
        f"- Composition receipt sha256: `{composition_receipt_sha256}`",
        "- Full-scope freshness: `false`; publication allowed: `false`",
        f"Closes #{issue_number}",
    )
    body_lines = set(body.splitlines())
    close_refs = re.findall(r"(?im)^[ \t]*(?:Closes|Fixes|Resolves)[ \t]+#([1-9][0-9]*)[ \t]*$", body)
    if any(line not in body_lines for line in required_body_lines) or close_refs != [str(issue_number)]:
        raise PromotionError("prepared PR recovery body does not bind the exact candidate and issue")
    if helper.body_marker(expected_owner, generation_id) not in body:
        raise PromotionError("prepared PR recovery body is missing the exact owner and generation marker")

    payload = candidate.get("payload_readback")
    if not isinstance(payload, Mapping) or any(payload.get(key) != value for key, value in {
        "status": "verified",
        "provider": "github-git-lfs",
        "repository": repository,
        "remote": "origin",
        "source_sha": candidate["head_sha"],
        "manifest_sha256": candidate["manifest_sha256"],
        "path": registry_path,
        "bytes": registry_bytes,
        "sha256": registry_sha256,
        "lfs_oid": registry_sha256,
        "readback": "isolated_lfs_storage_verified",
        "policy_path": "policy/registry-distribution.json",
    }.items()):
        raise PromotionError("prepared PR recovery lost the exact verified Git LFS payload proof")
    if (
        isinstance(payload.get("policy_bytes"), bool)
        or not isinstance(payload.get("policy_bytes"), int)
        or payload["policy_bytes"] < 1
        or not isinstance(payload.get("policy_sha256"), str)
        or not re.fullmatch(r"[a-f0-9]{64}", payload["policy_sha256"])
        or not isinstance(payload.get("observed_at"), str)
        or not payload["observed_at"]
    ):
        raise PromotionError("prepared PR recovery has an invalid Git LFS policy proof")

    checks = receipt.get("checks")
    if not isinstance(checks, Mapping) or any(checks.get(name) != "passed" for name in helper.REQUIRED_CHECKS):
        raise PromotionError("prepared PR recovery has incomplete source validation checks")
    manual_review = candidate.get("manual_review_acceptance_status")
    if manual_review not in helper.MANUAL_REVIEW_STATES or checks.get("manual_review_acceptance") != manual_review:
        raise PromotionError("prepared PR recovery changed the preserved manual-review status")
    applicability = checks.get("diagnostic_current_source_applicability")
    if applicability not in {"historical_scope_unchanged", "revalidation_required"}:
        raise PromotionError("prepared PR recovery has an invalid current-source applicability status")
    source_refresh = receipt.get("source_refresh_evidence")
    if not isinstance(source_refresh, Mapping) or any(source_refresh.get(key) != value for key, value in {
        "schema_version": "datapan.canonical-source-refresh-evidence.v1",
        "registry_path": registry_path,
        "registry_bytes": registry_bytes,
        "registry_sha256": registry_sha256,
        "manifest_sha256": candidate["manifest_sha256"],
    }.items()):
        raise PromotionError("prepared PR recovery lost the exact source-refresh validation receipt")
    source_applicability = source_refresh.get("diagnostic_current_source_applicability")
    if not isinstance(source_applicability, Mapping) or source_applicability.get("status") != applicability:
        raise PromotionError("prepared PR recovery changed source-applicability evidence")
    source_commands = source_refresh.get("commands")
    if not isinstance(source_commands, list) or len(source_commands) != 9 or any(
        not isinstance(item, Mapping)
        or item.get("exit_code") != 0
        or item.get("input_registry_sha256") != registry_sha256
        for item in source_commands
    ):
        raise PromotionError("prepared PR recovery has incomplete source-refresh command evidence")
    evidence = receipt.get("validation_evidence")
    required_evidence = set((*helper.REQUIRED_CHECKS, "manual_review_acceptance", "diagnostic_current_source_applicability"))
    if not isinstance(evidence, list) or any(
        not isinstance(item, Mapping)
        or item.get("exit_code") != 0
        or item.get("source_sha") != candidate["head_sha"]
        or item.get("manifest_sha256") != candidate["manifest_sha256"]
        for item in evidence
    ) or not required_evidence.issubset({
        item.get("name") for item in evidence
        if isinstance(item, Mapping) and isinstance(item.get("name"), str)
    }):
        raise PromotionError("prepared PR recovery lost the exact-head source-validation command evidence")

    related_open = [
        row for row in pull_requests
        if str(row.get("state", "")).upper() == "OPEN"
        and (
            row.get("owner_id") == expected_owner
            or row.get("generation_id") == generation_id
            or row.get("head_ref") == expected_branch
        )
    ]
    if len(related_open) != 1:
        raise PromotionError("prepared PR recovery requires exactly one open PR read-back for this owner and generation")
    row = related_open[0]
    number = row.get("number")
    if (
        isinstance(number, bool)
        or not isinstance(number, int)
        or number < 1
        or row.get("owner_id") != expected_owner
        or row.get("generation_id") != generation_id
        or row.get("head_ref") != expected_branch
        or row.get("base_ref") != "main"
        or row.get("head_sha") != candidate["head_sha"]
        or row.get("body") != body
        or row.get("body_sha256") != body_sha
        or (pending_pr_positive and number != pr_number)
        or (pending_pr_positive and row.get("url") != pr_record.get("url"))
    ):
        raise PromotionError("prepared PR recovery list read-back differs from the exact owned candidate")

    observed_body = observed.get("body")
    observed_number = observed.get("number")
    observed_merge = observed.get("mergeCommit")
    if observed_merge is None:
        observed_merge_sha = None
    elif (
        isinstance(observed_merge, Mapping)
        and isinstance(observed_merge.get("oid"), str)
        and re.fullmatch(r"[a-f0-9]{40}", observed_merge["oid"]) is not None
    ):
        observed_merge_sha = observed_merge["oid"]
    else:
        raise PromotionError("prepared PR recovery API returned a malformed merge commit identity")
    if (
        isinstance(observed_number, bool)
        or observed_number != number
        or observed.get("repository") != repository
        or observed.get("headRepository") != repository
        or str(observed.get("state", "")).upper() != "OPEN"
        or observed.get("headRefName") != expected_branch
        or observed.get("baseRefName") != "main"
        or observed.get("headRefOid") != candidate["head_sha"]
        or observed.get("url") != f"https://github.com/{repository}/pull/{number}"
        or not isinstance(observed_body, str)
        or observed_body != body
        or hashlib.sha256(observed_body.encode("utf-8")).hexdigest() != body_sha
    ):
        raise PromotionError("prepared PR recovery API read-back differs from exact repository, branch, head, body, owner, generation, or issue")
    if not isinstance(remote_branch_sha, str) or not re.fullmatch(r"[a-f0-9]{40}", remote_branch_sha) or remote_branch_sha != candidate["head_sha"]:
        raise PromotionError("prepared PR recovery remote branch SHA differs from the exact candidate head")
    return number


def bind_verified_recovery_head(receipt: Mapping[str, Any]) -> dict[str, Any]:
    """Advance only the ownership head after the caller completed strict remote read-back."""
    candidate = receipt.get("candidate")
    ownership = receipt.get("ownership")
    if not isinstance(candidate, Mapping) or not isinstance(ownership, Mapping):
        raise PromotionError("owned create recovery has no candidate ownership")
    head = candidate.get("head_sha")
    current = ownership.get("expected_head_sha")
    if not isinstance(head, str) or not re.fullmatch(r"[a-f0-9]{40}", head):
        raise PromotionError("owned create recovery candidate has no exact immutable head")
    if current not in {"0" * 40, head}:
        raise PromotionError("owned create recovery cannot normalize an arbitrary ownership head")
    if (
        receipt.get("status") not in {"prepared", "pending-review"}
        or receipt.get("action") not in {"create", "create_replacement"}
        or receipt.get("refresh_from") is not None
        or receipt.get("superseded_by") is not None
    ):
        raise PromotionError("owned create recovery cannot bind a refresh, superseded, or terminal receipt")
    updated = json.loads(json.dumps(receipt))
    updated["ownership"]["expected_head_sha"] = head
    return updated


def resolve_prepared_create_pr_readback(
    root: pathlib.Path,
    repository: str,
    receipt: Mapping[str, Any],
    helper: Any,
    *,
    source_id: str,
    scope: str,
    generation_id: str,
    registry_path: str,
    registry_bytes: int,
    registry_sha256: str,
    composition_receipt_sha256: str,
) -> tuple[int, dict[str, Any]]:
    """Read back one accepted create without regenerating or trusting CLI output."""
    candidate = receipt.get("candidate")
    ownership = receipt.get("ownership")
    if not isinstance(candidate, Mapping) or not isinstance(ownership, Mapping):
        raise PromotionError("prepared PR recovery receipt has no candidate ownership")
    action = receipt.get("action")
    if action not in {"create", "create_replacement"}:
        raise PromotionError("prepared PR recovery has an unsupported create action")
    try:
        owner = helper.owner_id(repository, source_id, scope)
        branch = helper.automation_branch(candidate, str(action))
    except Exception as exc:  # noqa: BLE001 - malformed durable identity must fail closed
        raise PromotionError("prepared PR recovery candidate ownership is invalid") from exc
    rows = gh_open_prs(root, repository)
    related_open = [
        row for row in rows
        if str(row.get("state", "")).upper() == "OPEN"
        and (
            row.get("owner_id") == owner
            or row.get("generation_id") == generation_id
            or row.get("head_ref") == branch
        )
    ]
    if len(related_open) != 1:
        raise PromotionError("prepared PR recovery found zero or multiple open owned PRs; no retry or mutation is safe")
    number = related_open[0].get("number")
    if isinstance(number, bool) or not isinstance(number, int) or number < 1:
        raise PromotionError("prepared PR recovery list read-back has no valid PR number")
    observed = gh_pr_readback(root, repository, number)
    materializer = helper.load_materializer(root)
    remote_sha = helper.remote_ref_sha(materializer, root, "origin", f"refs/heads/{branch}")
    validate_prepared_create_pr_readback(
        receipt, rows, observed, remote_sha, helper,
        repository=repository, source_id=source_id, scope=scope,
        generation_id=generation_id, registry_path=registry_path,
        registry_bytes=registry_bytes, registry_sha256=registry_sha256,
        composition_receipt_sha256=composition_receipt_sha256,
    )
    return number, observed


def prepared_create_validation_args(repository: str, receipt: Mapping[str, Any]) -> dict[str, Any]:
    """Build exact recovery inputs only from a validated durable candidate receipt."""
    candidate = receipt.get("candidate")
    if not isinstance(candidate, Mapping) or candidate.get("repository") != repository:
        raise PromotionError("prepared PR recovery candidate does not bind the current repository")
    for name in ("source_id", "scope", "generation_id", "registry_path", "registry_sha256", "composition_receipt_sha256"):
        if not isinstance(candidate.get(name), str) or not candidate[name]:
            raise PromotionError(f"prepared PR recovery candidate has no durable {name}")
    byte_count = candidate.get("registry_bytes")
    if isinstance(byte_count, bool) or not isinstance(byte_count, int) or byte_count < 1:
        raise PromotionError("prepared PR recovery candidate has no durable registry byte count")
    return {
        "source_id": candidate["source_id"],
        "scope": candidate["scope"],
        "generation_id": candidate["generation_id"],
        "registry_path": candidate["registry_path"],
        "registry_bytes": byte_count,
        "registry_sha256": candidate["registry_sha256"],
        "composition_receipt_sha256": candidate["composition_receipt_sha256"],
    }


def reconcile_prepared_create_pr(
    root: pathlib.Path,
    repository: str,
    receipt: Mapping[str, Any],
    helper: Any,
    *,
    source_id: str,
    scope: str,
    generation_id: str,
    registry_path: str,
    registry_bytes: int,
    registry_sha256: str,
    composition_receipt_sha256: str,
    journal_source_base_sha: str,
    observed_at: str,
    run_url: str,
    expected_state_sha: Any = STATE_EXPECTATION_UNSET,
    expected_target_main_sha: str | None = None,
) -> tuple[dict[str, Any], int]:
    """Bind a verified create head and preserve or append its exact PR acknowledgement."""
    number, observed = resolve_prepared_create_pr_readback(
        root, repository, receipt, helper,
        source_id=source_id, scope=scope, generation_id=generation_id,
        registry_path=registry_path, registry_bytes=registry_bytes,
        registry_sha256=registry_sha256,
        composition_receipt_sha256=composition_receipt_sha256,
    )
    bound = bind_verified_recovery_head(receipt)
    try:
        updated = helper.record_pr_readback(
            bound, observed,
            observed_at=observed_at, run_url=run_url,
        )
    except helper.AdmissionError as exc:
        raise PromotionError("owned PR recovery could not preserve or bind the exact pending-review acknowledgement") from exc
    expected = json.loads(json.dumps(receipt))
    expected["ownership"]["expected_head_sha"] = receipt["candidate"]["head_sha"]
    expected["pr"] = {
        "number": number,
        "url": f"https://github.com/{repository}/pull/{number}",
        "state": "open",
        "merge_commit_sha": (
            observed.get("mergeCommit", {}).get("oid")
            if isinstance(observed.get("mergeCommit"), Mapping)
            else None
        ),
    }
    if receipt.get("status") == "prepared":
        old_acknowledgements = receipt.get("acknowledgements", [])
        new_acknowledgements = updated.get("acknowledgements", [])
        if (
            updated.get("status") != "pending-review"
            or not isinstance(old_acknowledgements, list)
            or not isinstance(new_acknowledgements, list)
            or new_acknowledgements[:len(old_acknowledgements)] != old_acknowledgements
            or len(new_acknowledgements) != len(old_acknowledgements) + 1
        ):
            raise PromotionError("prepared PR recovery did not append exactly one pending-review acknowledgement")
        expected["status"] = "pending-review"
        expected["acknowledgements"] = new_acknowledgements
        pending_acknowledgements = [
            item for item in new_acknowledgements
            if isinstance(item, Mapping) and item.get("status") == "pending-review"
        ]
        if len(pending_acknowledgements) != 1 or not helper.exact_pending_review_witness(updated):
            raise PromotionError("prepared PR recovery acknowledgement does not bind exact candidate artifact/run provenance")
    elif updated.get("status") != "pending-review" or updated.get("acknowledgements") != receipt.get("acknowledgements"):
        raise PromotionError("pending-review recovery must preserve its historical acknowledgement without duplication")
    if updated != expected:
        raise PromotionError("owned PR recovery changed evidence beyond the verified expected-head binding and PR read-back")
    if expected_target_main_sha is not None:
        assert_remote_main_sha(root, expected_target_main_sha)
    persist_journal_record(
        root, journal_source_base_sha, updated, observed_at=observed_at,
        expected_state_sha=expected_state_sha,
    )
    return updated, number


def create_pr_then_reconcile_prepared_create(
    root: pathlib.Path,
    repository: str,
    receipt: Mapping[str, Any],
    helper: Any,
    *,
    source_id: str,
    scope: str,
    generation_id: str,
    registry_path: str,
    registry_bytes: int,
    registry_sha256: str,
    composition_receipt_sha256: str,
    journal_source_base_sha: str,
    observed_at: str,
    run_url: str,
    body_path: pathlib.Path,
    expected_state_sha: Any = STATE_EXPECTATION_UNSET,
    expected_target_main_sha: str | None = None,
) -> tuple[dict[str, Any], int]:
    """Attempt one PR create, then require authoritative read-back regardless of CLI status."""
    ownership = receipt.get("ownership")
    candidate = receipt.get("candidate")
    if not isinstance(ownership, Mapping) or not isinstance(candidate, Mapping):
        raise PromotionError("prepared PR creation has no durable owner or candidate")
    if expected_target_main_sha is not None:
        assert_remote_main_sha(root, expected_target_main_sha)
    try:
        command((
            "gh", "pr", "create", "--repo", repository, "--draft", "--base", "main",
            "--head", str(ownership.get("branch", "")), "--title", pr_title(candidate),
            "--body-file", str(body_path),
        ), root, allowed_returncodes=frozenset(range(-255, 256)))
    except (OSError, subprocess.SubprocessError, PromotionError):
        # A missing CLI result has the same safe recovery path as a nonzero
        # status: authoritative read-back either proves exact acceptance or
        # fails closed without creating another PR.
        pass
    return reconcile_prepared_create_pr(
        root, repository, receipt, helper,
        source_id=source_id, scope=scope, generation_id=generation_id,
        registry_path=registry_path, registry_bytes=registry_bytes,
        registry_sha256=registry_sha256,
        composition_receipt_sha256=composition_receipt_sha256,
        journal_source_base_sha=journal_source_base_sha,
        observed_at=observed_at, run_url=run_url,
        expected_state_sha=expected_state_sha,
        expected_target_main_sha=expected_target_main_sha,
    )


def verify_release_ci_observation(root: pathlib.Path, receipt: Mapping[str, Any]) -> tuple[str | None, str | None]:
    """Keep CI observation independent from the exact accepted PR read-back."""
    ci_state = None
    ci_blocker = None
    try:
        ci_receipt = ensure_verify_release_ci(root, receipt)
        ci_entry = ci_receipt.get("ci", {})
        if isinstance(ci_entry, Mapping):
            ci_state = ci_entry.get("state")
            ci_blocker = ci_entry.get("blocker")
    except Exception as exc:  # noqa: BLE001 - CI observation is independent of accepted PR ownership
        ci_blocker = str(exc)
    return ci_state, ci_blocker


def existing_pr_rows(
    journal: Mapping[str, Any] | None,
    github_prs: Sequence[Mapping[str, Any]],
    helper: Any,
    candidate: Mapping[str, Any],
) -> tuple[list[dict[str, Any]], int]:
    if not isinstance(journal, Mapping):
        owned = [row for row in github_prs if row.get("owner_id") == helper.owner_id(candidate["repository"], candidate["source_id"], candidate["scope"]) and row.get("state") == "open"]
        if owned:
            raise PromotionError("open_candidate_pr_without_durable_owner_receipt: preserve it and investigate state branch")
        return [], 0
    owner = helper.owner_id(candidate["repository"], candidate["source_id"], candidate["scope"])
    records = [
        row for row in journal.get("records", [])
        if row.get("ownership", {}).get("owner_id") == owner
    ]
    by_number = {int(row.get("number", 0)): row for row in github_prs}
    by_key = {helper.candidate_key(row): row for row in journal.get("records", []) if isinstance(row, Mapping)}
    result: list[dict[str, Any]] = []
    issue_number = 0
    recovered_revision_keys: set[tuple[str, str, str, str, str, str]] = set()
    open_numbers = {
        int(row.get("number", 0)) for row in github_prs
        if row.get("state") == "open" and row.get("owner_id") == owner
    }
    for number in sorted(open_numbers):
        observed = by_number.get(number)
        if observed is None:
            raise PromotionError("owned PR is absent from authoritative GitHub read-back")
        active_records = [
            row for row in records
            if row.get("superseded_by") is None
            and row.get("status") in {"prepared", "pending-review"}
            and int(row.get("pr", {}).get("number", 0)) == number
        ]
        if not active_records:
            recoveries = [
                row for row in records
                if row.get("superseded_by") is None
                and row.get("status") == "prepared"
                and int(row.get("pr", {}).get("number", 0)) == 0
                and row.get("candidate", {}).get("generation_id") == observed.get("generation_id")
                and row.get("candidate", {}).get("registry_sha256") == candidate.get("registry_sha256")
                and row.get("candidate", {}).get("head_sha") == observed.get("head_sha")
                and row.get("ownership", {}).get("body_sha256") == observed.get("body_sha256")
                and row.get("ownership", {}).get("branch") == observed.get("head_ref")
                and observed.get("base_ref") == "main"
                and observed.get("owner_id") == owner
                and helper.body_marker(owner, str(row.get("candidate", {}).get("generation_id", ""))) in str(observed.get("body", ""))
            ]
            if len(recoveries) != 1:
                raise PromotionError("interrupted candidate PR read-back is not one exact prepared head/body")
            selected = recoveries[0]
            recovered_revision_keys.add(helper.candidate_key(selected))
            issue_number = int(selected.get("ownership", {}).get("issue_number", 0))
            result.append({
                "repository": candidate["repository"], "source_id": candidate["source_id"], "scope": candidate["scope"],
                "state": "open", "owner_id": owner, "number": number,
                "head_sha": observed.get("head_sha"), "automation_head_sha": observed.get("head_sha"),
                "body_sha256": observed.get("body_sha256"), "automation_body_sha256": observed.get("body_sha256"),
                "head_ref": observed.get("head_ref"), "base_ref": observed.get("base_ref"),
                "candidate_head_sha": selected["candidate"]["head_sha"],
                "manifest_sha256": selected["candidate"]["manifest_sha256"],
                "registry_sha256": selected["candidate"]["registry_sha256"],
                "revision_ref": helper.revision_reference(selected),
            })
            continue
        direct = [
            row for row in active_records
            if (
                observed.get("head_sha") == row.get("candidate", {}).get("head_sha")
                and observed.get("body_sha256") == row.get("ownership", {}).get("body_sha256")
                and observed.get("owner_id") == owner
                and observed.get("generation_id") == row.get("candidate", {}).get("generation_id")
                and observed.get("head_ref") == row.get("ownership", {}).get("branch")
                and observed.get("base_ref") == "main"
            )
        ]
        selected: Mapping[str, Any] | None = None
        if len(direct) == 1:
            selected = direct[0]
        elif len(direct) > 1:
            raise PromotionError("duplicate durable revisions claim the exact current owned PR head/body")
        else:
            intents = [
                row for row in active_records
                if row.get("status") == "prepared" and isinstance(row.get("refresh_from"), Mapping)
            ]
            phases: list[tuple[str, Mapping[str, Any], Mapping[str, Any]]] = []
            for intent in intents:
                predecessor = by_key.get(helper._reference_key(intent["refresh_from"]))
                if predecessor is None:
                    raise PromotionError("prepared refresh intent has no exact durable predecessor")
                if int(intent.get("pr", {}).get("number", 0)) != number:
                    continue
                normalized = dict(observed)
                normalized.update({
                    "headRefName": observed.get("head_ref"),
                    "headRefOid": observed.get("head_sha"),
                    "baseRefName": observed.get("base_ref"),
                    "state": str(observed.get("state", "")).upper(),
                })
                phase = refresh_pr_phase(intent, predecessor, normalized, helper, require_repository=False)
                phases.append((phase, intent, predecessor))
            if len(phases) != 1:
                raise PromotionError("human_head_change_or_body_change: preserve the owned PR and stop")
            phase, intent, predecessor = phases[0]
            selected = predecessor if phase in {"before-push", "after-push-before-body"} else intent
        assert selected is not None
        row_state = "open"
        issue_number = int(selected.get("ownership", {}).get("issue_number", 0))
        result.append({
            "repository": candidate["repository"], "source_id": candidate["source_id"], "scope": candidate["scope"],
            "state": row_state, "owner_id": owner, "number": number,
            "head_sha": observed.get("head_sha"), "automation_head_sha": observed.get("head_sha"),
            "body_sha256": observed.get("body_sha256"), "automation_body_sha256": observed.get("body_sha256"),
            "head_ref": observed.get("head_ref"), "base_ref": observed.get("base_ref"),
            "candidate_head_sha": selected["candidate"]["head_sha"],
            "manifest_sha256": selected["candidate"]["manifest_sha256"],
            "registry_sha256": selected["candidate"]["registry_sha256"],
            "revision_ref": helper.revision_reference(selected),
        })
    closed_numbers = {
        int(row.get("number", 0)) for row in github_prs
        if row.get("state") == "closed" and row.get("owner_id") == owner
    }
    for number in sorted(closed_numbers):
        observed = by_number.get(number)
        matches = [
            row for row in records
            if row.get("superseded_by") is None
            and row.get("status") == "closed"
            and int(row.get("pr", {}).get("number", 0)) == number
            and row.get("candidate", {}).get("head_sha") == observed.get("head_sha")
            and row.get("ownership", {}).get("body_sha256") == observed.get("body_sha256")
            and observed.get("owner_id") == owner
            and observed.get("generation_id") == row.get("candidate", {}).get("generation_id")
        ]
        if len(matches) != 1:
            raise PromotionError("closed owned PR read-back does not bind one exact durable candidate revision")
        record = matches[0]
        result.append({
            "repository": candidate["repository"], "source_id": candidate["source_id"], "scope": candidate["scope"],
            "state": "closed", "owner_id": owner, "number": number,
            "head_sha": observed.get("head_sha"), "automation_head_sha": record["candidate"]["head_sha"],
            "body_sha256": observed.get("body_sha256"), "automation_body_sha256": record["ownership"]["body_sha256"],
            "candidate_head_sha": record["candidate"]["head_sha"],
            "manifest_sha256": record["candidate"]["manifest_sha256"],
            "registry_sha256": record["candidate"]["registry_sha256"],
            "revision_ref": helper.revision_reference(record),
        })
    for record in records:
        if record.get("superseded_by") is not None or record.get("status") not in {"prepared", "pending-review"}:
            continue
        if helper.candidate_key(record) in recovered_revision_keys:
            continue
        number = int(record.get("pr", {}).get("number", 0))
        if number:
            continue
        recoveries = [
            row for row in github_prs
            if row.get("state") == "open" and row.get("owner_id") == owner
            and row.get("generation_id") == record.get("candidate", {}).get("generation_id")
            and row.get("head_sha") == record.get("candidate", {}).get("head_sha")
            and row.get("body_sha256") == record.get("ownership", {}).get("body_sha256")
        ]
        if len(recoveries) > 1:
            raise PromotionError("duplicate_creation_readback: do not retry PR creation")
        if recoveries:
            observed = recoveries[0]
            result.append({
                "repository": candidate["repository"], "source_id": candidate["source_id"], "scope": candidate["scope"],
                "state": "open", "owner_id": owner, "number": int(observed["number"]),
                "head_sha": observed.get("head_sha"), "automation_head_sha": observed.get("head_sha"),
                "body_sha256": observed.get("body_sha256"), "automation_body_sha256": observed.get("body_sha256"),
                "head_ref": observed.get("head_ref"), "base_ref": observed.get("base_ref"),
                "candidate_head_sha": record["candidate"]["head_sha"],
                "manifest_sha256": record["candidate"]["manifest_sha256"],
                "registry_sha256": record["candidate"]["registry_sha256"],
                "revision_ref": helper.revision_reference(record),
            })
    opens = [row for row in result if row["state"] == "open"]
    if len(opens) > 1:
        raise PromotionError("duplicate_open_prs: preserve all candidate branches and resolve ownership")
    return result, issue_number


def active_open_pr_rows(existing: Sequence[Mapping[str, Any]]) -> list[Mapping[str, Any]]:
    """Separate the unique writable PR from closed archival ownership rows."""
    opened = [row for row in existing if row.get("state") == "open"]
    if len(opened) > 1:
        raise PromotionError("duplicate_open_prs: preserve all candidate branches and resolve ownership")
    return opened


def inspect_existing_pr_route(
    root: pathlib.Path,
    repository: str,
    journal: Mapping[str, Any] | None,
    existing: Sequence[Mapping[str, Any]],
    candidate: Mapping[str, Any],
    helper: Any,
) -> tuple[list[Mapping[str, Any]], list[Mapping[str, Any]], Mapping[str, Any] | None, Mapping[str, Any]]:
    """Resolve branch action from full history but read back only its unique open PR."""
    history = list(existing)
    decision = helper.decide_existing_prs(candidate, history)
    opened = active_open_pr_rows(existing)
    active_action = decision.get("action") in {"reuse_owned", "refresh_owned"}
    if bool(opened) != active_action:
        raise PromotionError("PR ownership route disagrees with the exact open-row inventory")
    if opened and int(decision.get("pr_number", 0)) != int(opened[0]["number"]):
        raise PromotionError("PR branch decision does not bind the unique open PR")
    observed = None
    if opened:
        observed = gh_pr_readback(root, repository, int(opened[0]["number"]))
        validate_existing_pr_api_readback(observed, journal, opened, helper)
    return history, opened, observed, decision


def pr_title(candidate: Mapping[str, Any]) -> str:
    return f"Review canonical registry update: {candidate['source_id']} ({candidate['generation_id']})"


def execute_candidate_preparation(args: argparse.Namespace, root: pathlib.Path) -> None:
    repo = os.environ["GITHUB_REPOSITORY"]
    run_id, attempt = str(args.workflow_run_id), str(args.workflow_run_attempt)
    if not re.fullmatch(r"[0-9]{6,20}", run_id) or not re.fullmatch(r"[1-9][0-9]*", attempt):
        raise PromotionError("processor workflow run/attempt identity is invalid")
    processor_run_id = f"{run_id}-{attempt}"
    bundle_dir = args.bundle_dir.resolve()
    idle = validate_no_candidate_processor_result(
        bundle_dir,
        repository=repo,
        workflow_run_id=run_id,
        workflow_run_attempt=attempt,
    )
    if idle is not None:
        print(json.dumps(idle, sort_keys=True))
        return
    helper = load_canonical_update_pr(root)
    composition_schema = load_object(root / "schemas/datapan.catalogue-composition-receipt.v1.schema.json")
    _, checkpoint = locate_processor_checkpoint(
        args.state_root, processor_run_id, repository=repo, workflow_run_id=run_id,
        artifact_id=args.processor_artifact_id,
        schema_path=root / "schemas/datapan.upstream-catalogue-checkpoint.v1.schema.json",
    )
    if checkpoint.get("source_id") != "data_go_kr" or checkpoint.get("source_scope") != "aggregate_supported_catalog":
        raise PromotionError("processor state is outside the admitted source/scope")
    validate_generation_identity(checkpoint)
    if not re.fullmatch(r"[a-f0-9]{40}", args.workflow_run_head_sha):
        raise PromotionError("processor workflow head must be a full immutable Git commit SHA")
    head_sha = command(("git", "rev-parse", "HEAD"), root).stdout.strip()
    explicit_predecessor = getattr(args, "source_refresh_predecessor", None)
    explicit_target_main = getattr(args, "source_refresh_target_main_sha", None)
    explicit_source_refresh = isinstance(explicit_predecessor, Mapping)
    canonical_context = getattr(args, "_authenticated_canonical_context", None)
    if not isinstance(canonical_context, Mapping) or not isinstance(canonical_context.get("identity"), Mapping):
        canonical_context = authenticated_current_canonical_context(root, head_sha)
    elif canonical_context["identity"].get("main_sha") != head_sha:
        raise PromotionError("caller-authenticated canonical context is not pinned to the preparation base")
    generation_inputs = checkpoint.get("generation_inputs", {})
    derivation = generation_inputs.get("same_observation_derivation") if isinstance(generation_inputs, Mapping) else None
    if explicit_source_refresh and derivation is not None:
        raise PromotionError("same-observation derivation cannot be combined with a trusted source-refresh predecessor")
    validation_journal: Mapping[str, Any] | None = None
    validation_journal_sha: str | None = None
    bundle = validate_processor_bundle(
        checkpoint, bundle_dir, composition_schema, helper, root=root,
        producer_head_sha=args.workflow_run_head_sha,
        canonical_context=canonical_context,
        allow_terminal_noop=not explicit_source_refresh,
        defer_seoul_declaration=True,
    )
    verify_processor_input_compatibility(
        root, checkpoint, args.workflow_run_head_sha, head_sha,
        composition_receipt=bundle.get("composition_receipt"),
    )
    if bundle.get("status") in {"ready", "no-change"}:
        validate_processor_seoul_bundle(
            checkpoint, bundle_dir, bundle, root=root,
            canonical_context=canonical_context,
            allow_terminal_noop=not explicit_source_refresh,
        )
    if (
        not explicit_source_refresh
        and bundle.get("status") in {"ready", "no-change"}
        and bundle_matches_current_canonical(bundle, canonical_context["identity"])
    ):
        outcome = checkpoint.get("outcome", {})
        print(json.dumps({
            "status": "already-canonical-payload",
            "reason": "candidate_matches_current_manifest_bound_registry",
            "candidate_available": False,
            "source_id": checkpoint["source_id"],
            "generation_id": checkpoint["generation_id"],
            "registry_sha256": bundle["registry_sha256"],
            "pending_count": outcome.get("pending_count"),
            "detail_retry_count": outcome.get("detail_retry_count"),
            "detail_unattempted_count": outcome.get("detail_unattempted_count"),
        }, sort_keys=True))
        return
    if derivation is not None:
        validation_journal, validation_journal_sha = load_promotion_journal_snapshot(root)
        validate_same_observation_derivation_for_c(
            root, args.state_root.resolve(), checkpoint, bundle,
            validation_journal, validation_journal_sha,
            current_head_sha=head_sha,
            canonical_identity=canonical_context["identity"],
            allow_terminal_noop=False,
        )
    if bundle.get("status") in {"retry", "quarantined"}:
        print(json.dumps({
            "status": "no-candidate", "reason": bundle["reason"],
            "processor_status": bundle["status"],
            "source_id": checkpoint["source_id"], "generation_id": checkpoint["generation_id"],
            "candidate_available": False,
        }, sort_keys=True))
        return
    if explicit_source_refresh:
        remote_main = command(("git", "ls-remote", "--heads", "origin", "refs/heads/main"), root)
        main_rows = [line.split("\t", 1)[0] for line in remote_main.stdout.splitlines() if line.endswith("\trefs/heads/main")]
        if main_rows != [head_sha]:
            raise PromotionError("stale_base: main changed after the upstream observation; request a fresh catalogue observation")
    else:
        canonical_identity = canonical_context["identity"]

    # The old immutable Hugging Face revision is materialized only to verify
    # the processor's precise observation baseline. Candidate preparation
    # later uses the declared Git LFS backend for the new OID.
    canonical_path = root / bundle["registry_path"]
    baseline = root / ".datapan/previous/data-go-kr.registry.json"
    baseline.parent.mkdir(parents=True, exist_ok=True)
    command((sys.executable, str(root / "scripts/materialize-canonical-registry.py"), "--output", str(baseline)), root)
    manifest = load_object(root / "manifest.json")
    registry_entries = [row for row in manifest.get("artifacts", []) if isinstance(row, dict) and row.get("path") == bundle["registry_path"] and row.get("kind") == "registry"]
    if len(registry_entries) != 1:
        raise PromotionError("release manifest does not have one canonical registry artifact")
    baseline_bytes, baseline_sha = registry_sha_from_path(baseline)
    if (baseline_bytes, baseline_sha) != (registry_entries[0].get("bytes"), bundle["baseline_sha256"]):
        raise PromotionError("stale_base: materialized source differs from the processor's immutable baseline; request a fresh observation")
    if bundle["registry_sha256"] == baseline_sha:
        print(json.dumps({"status": "no-change", "source_id": checkpoint["source_id"], "scope": checkpoint["source_scope"], "generation_id": checkpoint["generation_id"]}, sort_keys=True))
        return
    if command(("git", "status", "--porcelain", "--untracked-files=all"), root).stdout.strip():
        raise PromotionError("candidate checkout was not clean before staging; refusing to mix unrelated changes")

    prior_journal, journal_state_sha = load_promotion_journal_snapshot(root)
    if checkpoint.get("generation_inputs", {}).get("same_observation_derivation") is not None:
        validate_same_observation_derivation_for_c(
            root, args.state_root.resolve(), checkpoint, bundle,
            prior_journal, journal_state_sha,
            current_head_sha=head_sha,
            canonical_identity=canonical_identity,
            allow_terminal_noop=False,
        )
    if explicit_source_refresh and journal_state_sha != getattr(args, "source_refresh_expected_state_sha", None):
        raise PromotionError("promotion state changed while the trusted source refresh was preparing its exact B input")
    prior = None
    if explicit_source_refresh:
        if explicit_target_main != head_sha:
            raise PromotionError("trusted source refresh target main differs from the checked-out base")
        if (
            explicit_predecessor.get("candidate", {}).get("generation_id") != checkpoint.get("generation_id")
            or explicit_predecessor.get("candidate", {}).get("registry_sha256") != bundle.get("registry_sha256")
            or explicit_predecessor.get("candidate", {}).get("composition_receipt_sha256") != bundle.get("composition_receipt_sha256")
        ):
            raise PromotionError("source refresh does not preserve the predecessor B generation and composed payload")
        current_predecessor = journal_record_for(
            prior_journal,
            str(explicit_predecessor.get("candidate", {}).get("source_id", "")),
            str(explicit_predecessor.get("candidate", {}).get("scope", "")),
            str(explicit_predecessor.get("candidate", {}).get("generation_id", "")),
            str(explicit_predecessor.get("candidate", {}).get("registry_sha256", "")),
            candidate_head_sha=str(explicit_predecessor.get("candidate", {}).get("head_sha", "")),
        )
        helper = load_canonical_update_pr(root)
        if current_predecessor is None or not helper._reference_matches_receipt(
            helper.revision_reference(explicit_predecessor), current_predecessor,
        ):
            raise PromotionError("source refresh predecessor changed in the durable journal")
        if current_predecessor.get("superseded_by") is not None:
            raise PromotionError("source refresh predecessor was superseded after request validation")
        successor_head = getattr(args, "source_refresh_successor_head_sha", None)
        if successor_head:
            prior = journal_record_for(
                prior_journal, checkpoint["source_id"], checkpoint["source_scope"],
                checkpoint["generation_id"], bundle["registry_sha256"],
                candidate_head_sha=str(successor_head),
            )
            if (
                prior is None
                or prior.get("status") != "prepared"
                or prior.get("refresh_from") != helper.revision_reference(explicit_predecessor)
                or prior.get("refresh_target_main_sha") != explicit_target_main
            ):
                raise PromotionError("existing source refresh intent does not match the exact predecessor and target main")
    else:
        prior = journal_record_for(
            prior_journal, checkpoint["source_id"], checkpoint["source_scope"],
            checkpoint["generation_id"], bundle["registry_sha256"],
        )
    if prior is not None and prior.get("superseded_by") is not None:
        print(json.dumps({
            "status": "already-superseded", "generation_id": checkpoint["generation_id"],
            "registry_sha256": bundle["registry_sha256"],
        }, sort_keys=True))
        return
    if prior is not None and prior.get("status") not in {"prepared"} and not explicit_source_refresh:
        number = int(prior.get("pr", {}).get("number", 0))
        if prior.get("status") == "pending-review" and number > 0:
            observed = gh_pr_readback(root, repo, number)
            validate_exact_open_pr(prior, observed, helper)
        print(json.dumps({
            "status": "already-delivered", "pr_number": number,
            "generation_id": checkpoint["generation_id"],
            "registry_sha256": bundle["registry_sha256"],
        }, sort_keys=True))
        return
    prior_pr = prior.get("pr") if isinstance(prior, Mapping) else None
    if (
        not explicit_source_refresh
        and prior is not None
        and prior.get("status") == "prepared"
        and isinstance(prior_pr, Mapping)
        and prior_pr.get("number") == 0
    ):
        # Check the original immutable intent before any native regeneration.
        # A PR-zero row records prepared intent, not proof that branch advance
        # completed; only exact PR and remote-branch read-back can establish it.
        helper = load_canonical_update_pr(root)
        observed_at = dt.datetime.now(dt.timezone.utc).isoformat()
        run_url = f"https://github.com/{repo}/actions/runs/{os.environ['GITHUB_RUN_ID']}/attempts/{os.environ['GITHUB_RUN_ATTEMPT']}"
        recovery_args = {
            "source_id": checkpoint["source_id"], "scope": checkpoint["source_scope"],
            "generation_id": checkpoint["generation_id"], "registry_path": bundle["registry_path"],
            "registry_bytes": bundle["registry_bytes"], "registry_sha256": bundle["registry_sha256"],
            "composition_receipt_sha256": bundle["composition_receipt_sha256"],
        }
        if args.prepare_only:
            number, _ = resolve_prepared_create_pr_readback(
                root, repo, prior, helper, **recovery_args,
            )
            print(json.dumps({
                "status": "prepared-create-validated-read-only", "pr_number": number,
                "candidate_base_sha": prior["candidate"]["base_sha"],
                "candidate_sha": prior["candidate"]["head_sha"],
                "generation_id": prior["candidate"]["generation_id"],
                "external_mutations": False, "journal_updated": False,
                "verify_release_ci_state": None, "verify_release_ci_blocker": "prepare_only",
            }, sort_keys=True))
            return
        final_receipt, number = reconcile_prepared_create_pr(
            root, repo, prior, helper, **recovery_args,
            journal_source_base_sha=head_sha, observed_at=observed_at, run_url=run_url,
        )
        ci_state, ci_blocker = verify_release_ci_observation(root, final_receipt)
        print(json.dumps({
            "status": final_receipt["status"], "pr_number": number,
            "recovered_prepared_create": True, "candidate_base_sha": final_receipt["candidate"]["base_sha"],
            "candidate_sha": final_receipt["candidate"]["head_sha"],
            "registry_sha256": final_receipt["candidate"]["registry_sha256"],
            "generation_id": final_receipt["candidate"]["generation_id"],
            "manual_review_acceptance": final_receipt["candidate"]["manual_review_acceptance_status"],
            "blockers": final_receipt.get("blockers", []),
            "source_refresh_commands": len(final_receipt.get("source_refresh_evidence", {}).get("commands", [])),
            "native_source_regeneration": False,
            "verify_release_ci_state": ci_state, "verify_release_ci_blocker": ci_blocker,
        }, sort_keys=True))
        return
    if prior is None and isinstance(prior_journal, Mapping) and not explicit_source_refresh:
        same_payload = [
            row for row in prior_journal.get("records", [])
            if row.get("superseded_by") is None
            and row.get("status") == "pending-review"
            and row.get("candidate", {}).get("repository", "").casefold() == repo.casefold()
            and row.get("candidate", {}).get("source_id") == checkpoint["source_id"]
            and row.get("candidate", {}).get("scope") == checkpoint["source_scope"]
            and row.get("candidate", {}).get("registry_sha256") == bundle["registry_sha256"]
        ]
        if len(same_payload) > 1:
            raise PromotionError("multiple active PR revisions claim the same canonical payload digest")
        if same_payload:
            active = same_payload[0]
            number = int(active.get("pr", {}).get("number", 0))
            if number < 1:
                raise PromotionError("active same-payload candidate has no durable PR number")
            validate_exact_open_pr(active, gh_pr_readback(root, repo, number), helper)
            print(json.dumps({
                "status": "already-delivered", "pr_number": number,
                "generation_id": active["candidate"]["generation_id"],
                "registry_sha256": bundle["registry_sha256"],
            }, sort_keys=True))
            return

    canonical_path.write_bytes((bundle_dir / "composed-candidate.registry.json").read_bytes())
    helper.update_registry_manifest_artifact(root, pathlib.PurePosixPath(bundle["registry_path"]), bundle["registry_bytes"], bundle["registry_sha256"])
    old_published_pin = json.dumps(load_object(root / "policy/registry-distribution.json").get("canonical_registry"), sort_keys=True)
    review_artifacts = update_registry_review_artifacts(root, checkpoint["generation_id"], bundle_dir)
    refresh = load_module(root / "scripts/refresh-canonical-snapshot-evidence.py", "refresh_canonical_snapshot_evidence")
    observation = checkpoint.get("last_observation")
    stable_generated_at = observation.get("observed_at") if isinstance(observation, dict) else None
    if not isinstance(stable_generated_at, str) or not stable_generated_at:
        raise PromotionError("processor checkpoint has no source observation time for deterministic report regeneration")
    registry_sha, source_commands, source_refresh_evidence = refresh.run_source_refresh(
        repository_root=root, datapan_cli=args.datapan_cli.resolve(), registry=canonical_path,
        verification=root / "reports/latest-verification.json", previous_registry=baseline,
        stable_generated_at=stable_generated_at,
    )
    refresh.run_ledger_refresh(root)
    command((sys.executable, "scripts/refresh-release-ledger-evidence.py", "--check"), root)
    if registry_sha != bundle["registry_sha256"]:
        raise PromotionError("source refresh reports do not bind to the exact composed registry bytes")
    if json.dumps(load_object(root / "policy/registry-distribution.json").get("canonical_registry"), sort_keys=True) != old_published_pin:
        raise PromotionError("candidate preparation changed the immutable published Hugging Face identity pin")
    command((sys.executable, "scripts/generate-credential-runtime-manual-review-acceptance.py", "--check"), root)
    acceptance = manual_review_status(root)
    command((sys.executable, "scripts/sync-release-schema-artifacts.py", "--write"), root)
    command((sys.executable, "scripts/sync-release-manifest-artifacts.py", "--write"), root)
    source_refresh_evidence["manifest_sha256"] = file_sha256(root / "manifest.json")
    for native_output in source_refresh_evidence.get("commands", []):
        output = root / str(native_output["output_path"])
        if not output.is_file() or (output.stat().st_size, file_sha256(output)) != (native_output["output_bytes"], native_output["output_sha256"]):
            raise PromotionError("pinned native report changed after its source-bound output digest was recorded")

    # Recover an exact payload revision before making a commit. New registry
    # bytes under the same B generation form a distinct C revision and may
    # refresh the existing owned PR after exact read-back.
    staged_paths = stage_candidate_outputs(root, checkpoint["generation_id"])
    current_tree = command(("git", "write-tree"), root).stdout.strip()
    if prior is not None:
        prior_sha = prior["candidate"]["head_sha"]
        branch = prior.get("ownership", {}).get("branch")
        if isinstance(branch, str) and branch:
            materializer = helper.load_materializer(root)
            remote_branch = helper.remote_ref_sha(materializer, root, "origin", f"refs/heads/{branch}")
            if remote_branch is not None:
                command(("git", "fetch", "--no-tags", "origin", f"refs/heads/{branch}:refs/remotes/origin/canonical-update-candidate"), root)
        prior_tree_result = command(("git", "rev-parse", f"{prior_sha}^{{tree}}"), root, allowed_returncodes=frozenset({0, 128}))
        if prior_tree_result.returncode == 0:
            if prior_tree_result.stdout.strip() != current_tree:
                raise PromotionError("candidate_payload_revision_reused_with_different_tree: preserve the durable candidate receipt")
            command(("git", "checkout", "--detach", prior_sha), root)
        else:
            commit_env = dict(os.environ)
            commit_env["GIT_AUTHOR_DATE"] = stable_generated_at
            commit_env["GIT_COMMITTER_DATE"] = stable_generated_at
            command((
                "git", "-c", "user.name=datapan-canonical-update[bot]",
                "-c", "user.email=41898282+github-actions[bot]@users.noreply.github.com",
                "commit", "-m", f"Prepare canonical catalogue update {checkpoint['generation_id']} {bundle['registry_sha256']}",
            ), root, env=commit_env)
            rebuilt_sha = command(("git", "rev-parse", "HEAD"), root).stdout.strip()
            if rebuilt_sha != prior_sha:
                raise PromotionError("prepared candidate commit cannot be deterministically recovered from its durable refresh intent")
    else:
        commit_env = dict(os.environ)
        commit_env["GIT_AUTHOR_DATE"] = stable_generated_at
        commit_env["GIT_COMMITTER_DATE"] = stable_generated_at
        command((
            "git", "-c", "user.name=datapan-canonical-update[bot]",
            "-c", "user.email=41898282+github-actions[bot]@users.noreply.github.com",
            "commit", "-m", f"Prepare canonical catalogue update {checkpoint['generation_id']} {bundle['registry_sha256']}",
        ), root, env=commit_env)
    candidate_head = command(("git", "rev-parse", "HEAD"), root).stdout.strip()
    manifest_sha = file_sha256(root / "manifest.json")
    diagnostic_applicability_status = source_refresh_evidence["diagnostic_current_source_applicability"]["status"]
    candidate = {
        "repository": repo,
        "source_id": checkpoint["source_id"],
        "scope": checkpoint["source_scope"],
        "base_sha": head_sha,
        "head_sha": candidate_head,
        "manifest_sha256": manifest_sha,
        "registry_path": bundle["registry_path"],
        "registry_bytes": bundle["registry_bytes"],
        "registry_sha256": bundle["registry_sha256"],
        "composition_receipt_sha256": bundle["composition_receipt_sha256"],
        "composition_receipt": bundle["composition_receipt"],
        "composition_receipt_path": bundle["composition_receipt_path"],
        "composition_outputs_dir": bundle["composition_outputs_dir"],
        "generation_id": checkpoint["generation_id"],
        "checks": {
            "composition": "passed", "registry_manifest": "passed", "denominator": "passed",
            "adapter": "passed", "ledger": "passed", "release": "passed", "consumer": "passed",
            "finish_review_policy": finish_review_policy_status(root),
            "manual_review_acceptance": acceptance,
            "diagnostic_current_source_applicability": diagnostic_applicability_status,
        },
        "manual_review_acceptance_status": acceptance,
        "source_refresh_evidence": source_refresh_evidence,
    }
    evidence = run_bound_validation(root, args.datapan_cli.resolve(), candidate)
    candidate["validation_evidence"] = evidence

    if args.prepare_only:
        # Keep the offline rehearsal aligned with the pure local admission
        # gate that prepare_lfs_upload applies before any LFS operation.
        helper.validate_candidate(
            candidate, head_sha, composition_schema, require_payload_readback=False,
        )
        print(json.dumps({
            "status": "candidate-prepared-locally",
            "external_mutations": False,
            "repository": repo,
            "source_id": checkpoint["source_id"],
            "scope": checkpoint["source_scope"],
            "generation_id": checkpoint["generation_id"],
            "base_sha": head_sha,
            "candidate_sha": candidate_head,
            "manifest_sha256": manifest_sha,
            "registry_path": bundle["registry_path"],
            "registry_bytes": bundle["registry_bytes"],
            "registry_sha256": bundle["registry_sha256"],
            "manual_review_acceptance": acceptance,
            "validation_evidence_count": len(evidence),
            "source_refresh_evidence": source_refresh_evidence,
            "staged_paths": staged_paths,
        }, sort_keys=True))
        return

    github_prs = gh_open_prs(root, repo)
    existing, existing_issue_number = existing_pr_rows(prior_journal, github_prs, helper, candidate)
    # Keep closed rows intact for create_replacement and branch CAS decisions;
    # only the unique open row receives a writable PR API read-back.
    existing, open_existing, _, existing_decision = inspect_existing_pr_route(
        root, repo, prior_journal, existing, candidate, helper,
    )
    owner = helper.owner_id(repo, checkpoint["source_id"], checkpoint["source_scope"])
    # A refreshed generation reuses its still-open owned issue. Otherwise a
    # stable owner+generation marker makes interrupted issue creation safely
    # discoverable without duplicating it.
    policy_path = root / "policy/registry-distribution.json"
    previous_payload_readback = prior.get("candidate", {}).get("payload_readback") if prior is not None else None
    upload_lfs_oid = not (
        isinstance(previous_payload_readback, Mapping)
        and previous_payload_readback.get("status") == "verified"
        and previous_payload_readback.get("sha256") == bundle["registry_sha256"]
        and previous_payload_readback.get("source_sha") == candidate_head
    )
    prepared, _ = helper.prepare_lfs_upload(
        candidate, existing, head_sha, composition_schema, repository_root=root, policy_path=policy_path,
        remote="origin", upload=upload_lfs_oid,
    )
    if not upload_lfs_oid and isinstance(previous_payload_readback, Mapping):
        prepared["candidate"]["payload_readback"] = json.loads(json.dumps(previous_payload_readback))
    if open_existing:
        # Upload/read-back can take minutes. Re-read the actual PR API fields
        # after the remote LFS proof and compare them with the durable prior
        # receipt before advancing the branch.
        latest_prs = gh_open_prs(root, repo)
        latest_existing, _ = existing_pr_rows(prior_journal, latest_prs, helper, candidate)
        latest_existing, latest_open, _, latest_decision = inspect_existing_pr_route(
            root, repo, prior_journal, latest_existing, candidate, helper,
        )
        if len(latest_open) != 1 or latest_open[0]["number"] != open_existing[0]["number"]:
            raise PromotionError("owned PR changed during Git LFS validation; preserve branch and re-read")
        if latest_decision.get("branch") != existing_decision.get("branch"):
            raise PromotionError("owned PR branch route changed during Git LFS validation; preserve branch and re-read")
        existing = latest_existing
        open_existing = latest_open
    issue_number, issue_url = ensure_candidate_issue(root, repo, candidate, owner, existing_issue_number)
    body = helper.render_pr_body(candidate, owner, issue_number)
    prepared["ownership"]["issue_number"] = issue_number
    prepared["ownership"]["issue_url"] = issue_url
    prepared["ownership"]["body"] = body
    prepared["ownership"]["body_sha256"] = hashlib.sha256(body.encode("utf-8")).hexdigest()
    if explicit_source_refresh:
        prepared["refresh_from"] = helper.revision_reference(explicit_predecessor)
        prepared["refresh_target_main_sha"] = str(explicit_target_main)
    elif prior is not None and isinstance(prior.get("refresh_from"), Mapping):
        prepared["refresh_from"] = json.loads(json.dumps(prior["refresh_from"]))
    elif prepared.get("action") == "refresh_owned":
        if not open_existing or not isinstance(open_existing[0].get("revision_ref"), Mapping):
            raise PromotionError("owned PR refresh has no exact durable predecessor revision")
        prepared["refresh_from"] = json.loads(json.dumps(open_existing[0]["revision_ref"]))
    now = dt.datetime.now(dt.timezone.utc).isoformat()
    if explicit_source_refresh:
        assert_remote_main_sha(root, str(explicit_target_main))
    prepared_state_sha = persist_journal_record(
        root, head_sha, prepared, observed_at=now,
        expected_state_sha=journal_state_sha,
    )
    if open_existing:
        # This is the last GitHub PR read before the helper's remote-main and
        # candidate-branch compare-and-swap checks and branch push.
        latest_pr = gh_pr_readback(root, repo, int(open_existing[0]["number"]))
        if isinstance(prepared.get("refresh_from"), Mapping):
            predecessor = journal_record_for(
                prior_journal,
                str(prepared["refresh_from"].get("source_id", "")),
                str(prepared["refresh_from"].get("scope", "")),
                str(prepared["refresh_from"].get("generation_id", "")),
                str(prepared["refresh_from"].get("registry_sha256", "")),
                candidate_head_sha=str(prepared["refresh_from"].get("head_sha", "")),
            )
            if predecessor is None:
                raise PromotionError("prepared refresh intent lost its exact predecessor before branch CAS")
            refresh_pr_phase(prepared, predecessor, latest_pr, helper)
        else:
            validate_exact_open_pr(open_existing[0].get("record", prepared), latest_pr, helper)
    if explicit_source_refresh:
        assert_remote_main_sha(root, str(explicit_target_main))
    pushed = helper.push_owned_branch(
        candidate, prepared, existing, head_sha, repository_root=root, remote="origin",
    )
    body_path = root / ".datapan/candidate-pr.md"
    body_path.parent.mkdir(parents=True, exist_ok=True)
    body_path.write_text(body, encoding="utf-8")
    title = pr_title(candidate)
    if open_existing:
        number = int(open_existing[0]["number"])
        immediate = gh_pr_readback(root, repo, number)
        if isinstance(prepared.get("refresh_from"), Mapping):
            predecessor = journal_record_for(
                prior_journal,
                str(prepared["refresh_from"].get("source_id", "")),
                str(prepared["refresh_from"].get("scope", "")),
                str(prepared["refresh_from"].get("generation_id", "")),
                str(prepared["refresh_from"].get("registry_sha256", "")),
                candidate_head_sha=str(prepared["refresh_from"].get("head_sha", "")),
            )
            if predecessor is None:
                raise PromotionError("prepared refresh intent lost its exact predecessor after branch CAS")
            final_receipt = complete_prepared_refresh(
                root, repo, prepared, predecessor, helper, immediate,
                controller_head_sha=head_sha,
                journal_source_base_sha=head_sha,
                expected_state_sha=prepared_state_sha,
                observed_at=dt.datetime.now(dt.timezone.utc).isoformat(),
                run_url=f"https://github.com/{repo}/actions/runs/{os.environ['GITHUB_RUN_ID']}/attempts/{os.environ['GITHUB_RUN_ATTEMPT']}",
            )
        else:
            validate_exact_open_pr(prepared, immediate, helper)
            final_receipt = helper.record_pr_readback(
                pushed, immediate, observed_at=dt.datetime.now(dt.timezone.utc).isoformat(),
                run_url=f"https://github.com/{repo}/actions/runs/{os.environ['GITHUB_RUN_ID']}/attempts/{os.environ['GITHUB_RUN_ATTEMPT']}",
            )
            persist_journal_record(
                root, head_sha, final_receipt,
                observed_at=dt.datetime.now(dt.timezone.utc).isoformat(),
            )
    else:
        # The CLI may return nonzero after GitHub accepted the create request,
        # or lose its response. Its output is never the ownership authority.
        # Read back the unique exact candidate before recording pending-review.
        if explicit_source_refresh:
            assert_remote_main_sha(root, str(explicit_target_main))
        final_receipt, number = create_pr_then_reconcile_prepared_create(
            root, repo, pushed, helper,
            source_id=checkpoint["source_id"], scope=checkpoint["source_scope"],
            generation_id=checkpoint["generation_id"], registry_path=bundle["registry_path"],
            registry_bytes=bundle["registry_bytes"], registry_sha256=bundle["registry_sha256"],
            composition_receipt_sha256=bundle["composition_receipt_sha256"],
            journal_source_base_sha=head_sha,
            observed_at=dt.datetime.now(dt.timezone.utc).isoformat(),
            run_url=f"https://github.com/{repo}/actions/runs/{os.environ['GITHUB_RUN_ID']}/attempts/{os.environ['GITHUB_RUN_ATTEMPT']}",
            body_path=body_path,
            expected_state_sha=(prepared_state_sha if explicit_source_refresh else STATE_EXPECTATION_UNSET),
            expected_target_main_sha=(str(explicit_target_main) if explicit_source_refresh else None),
        )
    ci_state, ci_blocker = verify_release_ci_observation(root, final_receipt)
    print(json.dumps({
        "status": final_receipt["status"], "pr_number": number,
        "candidate_sha": candidate_head, "registry_sha256": bundle["registry_sha256"],
        "generation_id": checkpoint["generation_id"], "manual_review_acceptance": acceptance,
        "blockers": final_receipt.get("blockers", []), "source_refresh_commands": len(source_commands),
        "verify_release_ci_state": ci_state, "verify_release_ci_blocker": ci_blocker,
        "staged_path_count": len(staged_paths),
        "review_artifacts": str(review_artifacts.relative_to(root)),
    }, sort_keys=True))


def recover_ready_processor_candidate(args: argparse.Namespace, root: pathlib.Path) -> dict[str, Any]:
    """Recover at most one durable B ready artifact using current trusted C code."""
    repository = os.environ["GITHUB_REPOSITORY"]
    default_branch = (
        os.environ.get("GITHUB_DEFAULT_BRANCH")
        or os.environ.get("DEFAULT_BRANCH")
        or os.environ.get("GITHUB_REF_NAME")
        or "main"
    )
    schema_path = root / "schemas/datapan.upstream-catalogue-checkpoint.v1.schema.json"
    if not args.state_root or not args.datapan_cli:
        raise PromotionError("ready recovery requires the durable B state branch and pinned Datapan CLI")

    # A workflow_run event is an additional trusted notification, not the
    # authority for selecting a candidate. The durable checkpoint's own run
    # and artifact locator are independently verified below.
    event_run_id = args.event_run_id
    if event_run_id:
        if not args.event_run_attempt or not args.event_head_sha:
            raise PromotionError("B workflow_run notification is missing its exact attempt or head SHA")
        if not re.fullmatch(r"[0-9]{6,20}", event_run_id):
            raise PromotionError("B workflow_run notification has an invalid run id")
        event_run = processor_run_api(root, repository, event_run_id, args.event_run_attempt)
        validate_trusted_processor_run(
            event_run, repository=repository, run_id=event_run_id,
            attempt=args.event_run_attempt, default_branch=default_branch,
            expected_head_sha=args.event_head_sha,
        )

    journal, journal_ref_sha = load_promotion_journal_snapshot(root)
    candidates, blocked = list_recoverable_processor_checkpoints(
        args.state_root.resolve(), schema_path, journal,
    )
    terminal_generations: list[dict[str, Any]] = [
        {
            "generation_id": row.get("generation_id") if re.fullmatch(r"[a-f0-9]{64}", str(row.get("generation_id", ""))) else None,
            "checkpoint_sha256": None,
            "source_id": "data_go_kr",
            "source_scope": "aggregate_supported_catalog",
            "status": "blocked",
            "reason_code": row.get("reason") if isinstance(row.get("reason"), str) and re.fullmatch(r"[a-z0-9_]{1,96}", row.get("reason", "")) else "processor_checkpoint_unavailable",
            "producer": {"run_id": None, "run_attempt": None, "head_sha": None, "artifact_id": None, "artifact_name": None, "artifact_expires_at": None, "bundle_manifest_sha256": None},
            "checkpoint_observation": None,
            "checkpoint_observation_count": None,
            "generation_baseline_sha256": None,
            "checkpoint_derivation_sha256": None,
            "composition": None,
            "pending_count": None,
            "detail_retry_count": None,
            "detail_unattempted_count": None,
        }
        for row in blocked
    ]
    if not candidates:
        result = terminal_recovery_result(
            status="no-eligible-ready-processor-bundle" if blocked else "no-undelivered-ready-processor-bundle",
            candidate_available=False, generations=terminal_generations,
        )
        print(json.dumps({
            "status": result["status"],
            "candidate_available": False,
            "blocked_generations": blocked,
        }, sort_keys=True))
        return result

    composition_schema = load_object(root / "schemas/datapan.catalogue-composition-receipt.v1.schema.json")
    composition_helper = load_canonical_update_pr(root)
    current_head_sha = command(("git", "rev-parse", "HEAD"), root).stdout.strip()
    already_canonical: list[dict[str, Any]] = []
    terminal_context: dict[str, Any] = {}
    screened, blocked = select_first_eligible_processor_bundle(
        root, repository, candidates, blocked,
        journal=journal,
        journal_ref_sha=journal_ref_sha,
        state_root=args.state_root.resolve(),
        default_branch=default_branch,
        current_head_sha=current_head_sha,
        composition_schema=composition_schema,
        composition_helper=composition_helper,
        already_canonical=already_canonical,
        terminal_generations=terminal_generations,
        terminal_context=terminal_context,
    )
    if screened is None:
        all_generations_canonical = (
            bool(terminal_generations)
            and len(terminal_generations) <= MAX_TERMINAL_OUTCOME_GENERATIONS
            and all(row.get("status") == "already_canonical" for row in terminal_generations)
        )
        status = "already-canonical-payload" if all_generations_canonical else "no-eligible-ready-processor-bundle"
        result = terminal_recovery_result(
            status=status,
            candidate_available=False,
            generations=terminal_generations,
            evaluated_main=terminal_context.get("evaluated_main"),
        )
        print(json.dumps({
            "status": status,
            "candidate_available": False,
            "blocked_generations": blocked,
            "already_canonical_generations": already_canonical,
        }, sort_keys=True))
        return result
    if already_canonical:
        print(json.dumps({
            "status": "skipped-already-canonical-processor-bundles",
            "candidate_available": False,
            "already_canonical_generations": already_canonical,
        }, sort_keys=True))
    if blocked:
        print(json.dumps({"status": "skipped-unusable-older-generations", "blocked_generations": blocked}, sort_keys=True))
    args.bundle_dir = screened["bundle_dir"]
    args.workflow_run_id = screened["run_id"]
    args.workflow_run_attempt = screened["attempt"]
    args.workflow_run_head_sha = str(screened["run"]["head_sha"])
    args.processor_artifact_id = screened["artifact_id"]
    args._authenticated_canonical_context = screened.get("canonical_context")
    execute_candidate_preparation(args, root)
    return terminal_recovery_result(
        status="candidate_preparation_returned",
        candidate_available=True,
        generations=terminal_generations,
        evaluated_main=terminal_context.get("evaluated_main"),
        selected_generation_id=str(screened.get("generation_id", "")),
        preparation_returned=True,
    )


def reconcile_open_promotions(root: pathlib.Path, *, prepare_only: bool = False) -> None:
    repo = os.environ["GITHUB_REPOSITORY"]
    helper = load_module(root / "scripts/canonical_update_pr.py", "canonical_update_pr_reconcile")
    journal, journal_state_sha = load_promotion_journal_snapshot(root)
    if not isinstance(journal, Mapping):
        print(json.dumps({"status": "no-promotion-journal"}))
        return
    run_url = f"https://github.com/{repo}/actions/runs/{os.environ['GITHUB_RUN_ID']}/attempts/{os.environ['GITHUB_RUN_ATTEMPT']}"
    base = command(("git", "rev-parse", "HEAD"), root).stdout.strip()
    prepared_create_intents = [
        intent for intent in journal.get("records", [])
        if isinstance(intent, Mapping)
        and intent.get("status") == "prepared"
        and intent.get("superseded_by") is None
        and not isinstance(intent.get("refresh_from"), Mapping)
        and isinstance(intent.get("pr"), Mapping)
        and intent["pr"].get("number") == 0
    ]
    pending_create_receipts = [
        receipt for receipt in journal.get("records", [])
        if isinstance(receipt, Mapping)
        and receipt.get("status") == "pending-review"
        and receipt.get("superseded_by") is None
        and isinstance(receipt.get("pr"), Mapping)
        and isinstance(receipt.get("ownership"), Mapping)
        and isinstance(receipt["pr"].get("number"), int)
        and not isinstance(receipt["pr"].get("number"), bool)
        and receipt["pr"].get("number", 0) > 0
        and receipt["ownership"].get("expected_head_sha") != receipt.get("candidate", {}).get("head_sha")
    ]
    if prepare_only:
        readbacks = []
        for intent in prepared_create_intents:
            validation_args = prepared_create_validation_args(repo, intent)
            number, _ = resolve_prepared_create_pr_readback(
                root, repo, intent, helper, **validation_args,
            )
            readbacks.append({
                "pr_number": number,
                "candidate_base_sha": intent["candidate"]["base_sha"],
                "candidate_sha": intent["candidate"]["head_sha"],
                "generation_id": intent["candidate"]["generation_id"],
            })
        pending_bindings = []
        for receipt in pending_create_receipts:
            validation_args = prepared_create_validation_args(repo, receipt)
            number, _ = resolve_prepared_create_pr_readback(
                root, repo, receipt, helper, **validation_args,
            )
            pending_bindings.append({
                "pr_number": number,
                "candidate_base_sha": receipt["candidate"]["base_sha"],
                "candidate_sha": receipt["candidate"]["head_sha"],
                "generation_id": receipt["candidate"]["generation_id"],
                "head_binding_required": True,
            })
        print(json.dumps({
            "status": "promotion-pr-recovery-validated-read-only",
            "validated_prepared_creates": readbacks,
            "validated_pending_create_head_bindings": pending_bindings,
            "external_mutations": False,
            "journal_updated": False,
            "ci_dispatched": False,
        }, sort_keys=True))
        return
    reconciled_create_keys: set[tuple[str, str, str, str, str]] = set()
    for intent in prepared_create_intents:
        validation_args = prepared_create_validation_args(repo, intent)
        observed_at = dt.datetime.now(dt.timezone.utc).isoformat()
        refreshed, recovered_number = reconcile_prepared_create_pr(
            root, repo, intent, helper, **validation_args,
            journal_source_base_sha=base, observed_at=observed_at, run_url=run_url,
        )
        ci_state, ci_blocker = verify_release_ci_observation(root, refreshed)
        print(json.dumps({
            "status": "prepared-pr-create-recovered",
            "pr_number": recovered_number,
            "candidate_base_sha": refreshed["candidate"]["base_sha"],
            "candidate_sha": refreshed["candidate"]["head_sha"],
            "generation_id": refreshed["candidate"]["generation_id"],
            "verify_release_ci_state": ci_state,
            "verify_release_ci_blocker": ci_blocker,
        }, sort_keys=True))
        reconciled_create_keys.add(helper.candidate_key(intent))
        journal, journal_state_sha = load_promotion_journal_snapshot(root)
        if not isinstance(journal, Mapping):
            raise PromotionError("promotion journal disappeared after prepared PR creation recovery")
    # Complete only refreshes whose persisted intent, predecessor identity,
    # target head, and target body prove one of the two post-push crash points.
    # A stale pre-push intent is an isolated blocker for that predecessor and
    # must not prevent unrelated owned PRs from being reconciled.
    for intent in list(journal.get("records", [])):
        if (
            intent.get("status") != "prepared"
            or intent.get("superseded_by") is not None
            or not isinstance(intent.get("refresh_from"), Mapping)
            or int(intent.get("pr", {}).get("number", 0)) < 1
        ):
            continue
        predecessor_ref = intent["refresh_from"]
        predecessor = journal_record_for(
            journal,
            str(predecessor_ref.get("source_id", "")),
            str(predecessor_ref.get("scope", "")),
            str(predecessor_ref.get("generation_id", "")),
            str(predecessor_ref.get("registry_sha256", "")),
            candidate_head_sha=str(predecessor_ref.get("head_sha", "")),
        )
        if predecessor is None:
            raise PromotionError("prepared refresh intent has no exact predecessor revision")
        number = int(intent["pr"]["number"])
        target_main = intent.get("refresh_target_main_sha")
        observed = gh_pr_readback(root, repo, number)
        phase = refresh_pr_phase(intent, predecessor, observed, helper)
        if phase == "before-push":
            if isinstance(target_main, str) and target_main != base:
                print(json.dumps({
                    "status": "prepared-source-refresh-stale-before-push",
                    "pr_number": number,
                    "generation_id": intent.get("candidate", {}).get("generation_id"),
                    "target_main_sha": target_main,
                    "current_main_sha": base,
                    "action": "preserve intent; explicit retirement and re-request required before retargeting",
                }, sort_keys=True))
            else:
                print(json.dumps({
                    "status": "prepared-source-refresh-awaiting-explicit-push",
                    "pr_number": number,
                    "generation_id": intent.get("candidate", {}).get("generation_id"),
                    "target_main_sha": target_main,
                }, sort_keys=True))
            continue
        try:
            refreshed = complete_prepared_refresh(
                root, repo, intent, predecessor, helper, observed,
                controller_head_sha=base,
                journal_source_base_sha=base,
                expected_state_sha=journal_state_sha,
                observed_at=dt.datetime.now(dt.timezone.utc).isoformat(),
                run_url=run_url,
            )
        except PromotionError as exc:
            if "trusted source refresh target main moved" not in str(exc):
                raise
            print(json.dumps({
                "status": "prepared-source-refresh-waiting-for-current-main",
                "pr_number": number,
                "generation_id": intent.get("candidate", {}).get("generation_id"),
                "target_main_sha": target_main,
                "current_main_sha": base,
                "reason": str(exc),
            }, sort_keys=True))
            continue
        print(json.dumps({
            "status": "prepared-source-refresh-recovered",
            "pr_number": number,
            "generation_id": refreshed.get("candidate", {}).get("generation_id"),
            "candidate_sha": refreshed.get("candidate", {}).get("head_sha"),
            "target_main_sha": target_main,
        }, sort_keys=True))
        journal, journal_state_sha = load_promotion_journal_snapshot(root)
        if not isinstance(journal, Mapping):
            raise PromotionError("promotion journal disappeared after prepared source refresh recovery")

    prs = gh_open_prs(root, repo)
    by_number = {int(row["number"]): row for row in prs}
    for old in list(journal.get("records", [])):
        if helper.candidate_key(old) in reconciled_create_keys:
            continue
        if (
            old.get("superseded_by") is not None
            or (old.get("status") == "prepared" and isinstance(old.get("refresh_from"), Mapping))
            or old.get("status") not in {"prepared", "pending-review"}
        ):
            continue
        number = int(old.get("pr", {}).get("number", 0))
        if number == 0:
            raise PromotionError("non-prepared promotion journal row has no durable PR number")
        actual = by_number.get(number)
        if actual is None:
            raise PromotionError("journal candidate PR is absent from GitHub read-back")
        observed = gh_pr_readback(root, repo, number)
        if observed.get("state") == "OPEN":
            standalone_create = (
                old.get("status") == "pending-review"
                and old.get("action") in {"create", "create_replacement"}
                and old.get("refresh_from") is None
                and old.get("superseded_by") is None
            )
            candidate = old.get("candidate", {})
            ownership = old.get("ownership", {})
            if standalone_create:
                validation_args = prepared_create_validation_args(repo, old)
                refreshed, verified_number = reconcile_prepared_create_pr(
                    root, repo, old, helper, **validation_args,
                    journal_source_base_sha=base,
                    observed_at=dt.datetime.now(dt.timezone.utc).isoformat(),
                    run_url=run_url,
                )
                if verified_number != number:
                    raise PromotionError("owned pending-review PR changed number during expected-head recovery")
            elif ownership.get("expected_head_sha") != candidate.get("head_sha"):
                raise PromotionError("zero or mismatched expected head is not recoverable for a refresh, predecessor, or non-create receipt")
            else:
                refreshed = helper.record_pr_readback(
                    old, observed, observed_at=dt.datetime.now(dt.timezone.utc).isoformat(), run_url=run_url,
                )
                persist_journal_record(root, base, refreshed, observed_at=dt.datetime.now(dt.timezone.utc).isoformat())
            try:
                ci_receipt = ensure_verify_release_ci(root, refreshed)
                ci = ci_receipt.get("ci", {})
                print(json.dumps({
                    "status": "verify-release-ci-reconciled",
                    "pr_number": number,
                    "ci_state": ci.get("state") if isinstance(ci, Mapping) else None,
                    "ci_blocker": ci.get("blocker") if isinstance(ci, Mapping) else None,
                }, sort_keys=True))
            except Exception as exc:  # noqa: BLE001 - CI observation must not gate independent candidate recovery
                print(json.dumps({
                    "status": "verify-release-ci-observation-failed",
                    "pr_number": number,
                    "reason": str(exc),
                }, sort_keys=True))
            continue
        new = helper.record_pr_readback(
            old, observed, observed_at=dt.datetime.now(dt.timezone.utc).isoformat(), run_url=run_url,
        )
        persist_journal_record(root, base, new, observed_at=dt.datetime.now(dt.timezone.utc).isoformat())
    print(json.dumps({"status": "promotion-prs-reconciled", "run_url": run_url}, sort_keys=True))


def reconcile_publication(
    root: pathlib.Path,
    receipt_path: pathlib.Path,
    *,
    publisher_reference: str | None = None,
    expected_pr_number: int | None = None,
    before_journal_write: Any | None = None,
    after_journal_write: Any | None = None,
    journal_snapshot: tuple[Mapping[str, Any], str] | None = None,
    pr_readback: Any | None = None,
) -> dict[str, Any]:
    repo = os.environ["GITHUB_REPOSITORY"]
    helper = load_module(root / "scripts/canonical_update_pr.py", "canonical_update_publication_reconcile")
    publication = load_object(receipt_path)
    source = publication.get("source_binding", {})
    if not isinstance(source, Mapping):
        raise PromotionError("#592 publication receipt has no source-binding object")
    source_sha = source.get("source_sha")
    manifest_sha = source.get("manifest_sha256")
    if publication.get("schema_version") != "datapan.registry-publication-receipt.v1":
        raise PromotionError("unsupported #592 publication receipt schema")
    if (
        not isinstance(source_sha, str) or not re.fullmatch(r"[0-9a-f]{40}", source_sha)
        or not isinstance(manifest_sha, str) or not re.fullmatch(r"[0-9a-f]{64}", manifest_sha)
        or source.get("repository") != repo
    ):
        raise PromotionError("#592 receipt has no exact current-repository source and manifest binding")
    if journal_snapshot is None:
        journal, state_sha = load_promotion_journal_snapshot(root)
    else:
        journal, state_sha = journal_snapshot
        if not isinstance(journal, Mapping) or not re.fullmatch(r"[a-f0-9]{40}", state_sha):
            raise PromotionError("publication recovery journal snapshot is invalid")
    if not isinstance(journal, Mapping):
        raise PromotionError("publication receipt has no matching durable promotion journal")
    manifest_candidates = [
        row for row in journal.get("records", [])
        if row.get("candidate", {}).get("manifest_sha256") == manifest_sha
    ]
    tracked, pr = select_publication_candidate(
        manifest_candidates,
        source_sha=source_sha,
        readback=pr_readback or (lambda number: gh_pr_readback(root, repo, number)),
        expected_pr_number=expected_pr_number,
    )
    run_url = f"https://github.com/{repo}/actions/runs/{os.environ['GITHUB_RUN_ID']}/attempts/{os.environ['GITHUB_RUN_ATTEMPT']}"
    observed_at = dt.datetime.now(dt.timezone.utc).isoformat()
    if tracked.get("status") == "read-back-confirmed":
        # Authenticate every immutable publication identity through the
        # existing reconciliation rules, while keeping a repeated receipt a
        # true zero-write no-op.
        readback = helper.record_pr_readback(
            tracked, pr, observed_at=observed_at, run_url=run_url,
        )
        if readback != tracked:
            raise PromotionError("already-acknowledged publication PR read-back changed durable state")
        checked = helper.reconcile_huggingface_publication(
            tracked, receipt_path, observed_at=observed_at, run_url=run_url,
            publisher_reference=publisher_reference,
        )
        if checked != tracked:
            raise PromotionError("already-acknowledged publication receipt unexpectedly changed durable state")
        print(json.dumps({
            "status": "already_acknowledged",
            "source_sha": source_sha,
            "manifest_sha256": manifest_sha,
            "run_url": run_url,
            "journal_writes": 0,
        }, sort_keys=True))
        return {
            "status": "already_acknowledged",
            "source_sha": source_sha,
            "manifest_sha256": manifest_sha,
            "journal_writes": 0,
        }
    observed = helper.record_pr_readback(tracked, pr, observed_at=observed_at, run_url=run_url)
    if observed.get("pr", {}).get("state") != "merged" or observed.get("pr", {}).get("merge_commit_sha") != source_sha:
        raise PromotionError("#592 publication source is not the exact merge commit independently read from the canonical candidate PR")
    # Validate the complete receipt and immutable publication/read-back chain
    # before persisting even the intermediate live merge acknowledgement.
    updated = helper.reconcile_huggingface_publication(
        observed, receipt_path, observed_at=observed_at, run_url=run_url,
        publisher_reference=publisher_reference,
    )
    controller_sha = command(("git", "rev-parse", "HEAD"), root).stdout.strip()
    state_after_merge = state_sha
    writes = 0
    if observed != tracked:
        if before_journal_write is not None:
            before_journal_write()
        state_after_merge = persist_journal_record(
            root, controller_sha, observed, observed_at=observed_at,
            expected_state_sha=state_sha,
        )
        writes += 1
        if after_journal_write is not None:
            after_journal_write(state_after_merge)
    if updated != observed:
        if before_journal_write is not None:
            before_journal_write()
        state_after_publication = persist_journal_record(
            root, controller_sha, updated, observed_at=observed_at,
            expected_state_sha=state_after_merge,
        )
        writes += 1
        if after_journal_write is not None:
            after_journal_write(state_after_publication)
    print(json.dumps({"status": updated["status"], "source_sha": source_sha, "manifest_sha256": manifest_sha, "run_url": run_url}, sort_keys=True))
    return {
        "status": updated["status"],
        "source_sha": source_sha,
        "manifest_sha256": manifest_sha,
        "journal_writes": writes,
    }


def select_publication_candidate(
    candidates: list[Mapping[str, Any]],
    *,
    source_sha: str,
    readback: Any,
    expected_pr_number: int | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Resolve #592 against live PR API state, including before hourly PR reconciliation."""
    if expected_pr_number is not None and (
        isinstance(expected_pr_number, bool)
        or not isinstance(expected_pr_number, int)
        or expected_pr_number < 1
    ):
        raise PromotionError("publication expected PR number is invalid")
    matches: list[tuple[dict[str, Any], dict[str, Any]]] = []
    for candidate in candidates:
        if not isinstance(candidate, Mapping):
            raise PromotionError("promotion journal contains a malformed candidate row")
        pr_record = candidate.get("pr")
        number = pr_record.get("number") if isinstance(pr_record, Mapping) else None
        if isinstance(number, bool) or not isinstance(number, int) or number < 1:
            continue
        if expected_pr_number is not None and number != expected_pr_number:
            continue
        pr = readback(number)
        merge = pr.get("mergeCommit")
        merge_sha = merge.get("oid") if isinstance(merge, Mapping) else None
        if pr.get("state") == "MERGED" and merge_sha == source_sha:
            matches.append((dict(candidate), dict(pr)))
    if len(matches) != 1:
        raise PromotionError("#592 receipt does not identify exactly one merged canonical candidate by live PR read-back")
    return matches[0]


def journal_record_for(
    journal: Mapping[str, Any] | None,
    source_id: str,
    scope: str,
    generation_id: str | None = None,
    registry_sha256: str | None = None,
    *,
    candidate_head_sha: str | None = None,
) -> dict[str, Any] | None:
    if not isinstance(journal, Mapping):
        return None
    rows = [
        row for row in journal.get("records", [])
        if isinstance(row, dict)
        and row.get("candidate", {}).get("source_id") == source_id
        and row.get("candidate", {}).get("scope") == scope
        and (generation_id is None or row.get("candidate", {}).get("generation_id") == generation_id)
        and (registry_sha256 is None or row.get("candidate", {}).get("registry_sha256") == registry_sha256)
        and (candidate_head_sha is None or row.get("candidate", {}).get("head_sha") == candidate_head_sha)
    ]
    if candidate_head_sha is not None:
        if len(rows) > 1:
            raise PromotionError("promotion journal has duplicate exact source revision entries")
        return rows[0] if rows else None
    if registry_sha256 is not None and len(rows) > 1:
        active = [row for row in rows if row.get("superseded_by") is None]
        if len(active) == 1:
            return active[0]
        raise PromotionError("promotion journal payload lookup is ambiguous; exact candidate head is required")
    if registry_sha256 is not None and rows:
        active = [row for row in rows if row.get("superseded_by") is None]
        if len(active) == 1:
            return active[0]
        if len(active) > 1:
            raise PromotionError("promotion journal payload lookup is ambiguous; exact candidate head is required")
        return rows[-1]
    if generation_id is not None and registry_sha256 is None and len(rows) > 1:
        raise PromotionError("promotion journal revision lookup requires the exact registry payload digest")
    return rows[-1] if rows else None


def run_source_refresh(args: argparse.Namespace, root: pathlib.Path) -> None:
    """Refresh one adopted owned PR from the same durable B payload on trusted main."""
    repository = os.environ.get("GITHUB_REPOSITORY", "")
    default_branch = os.environ.get("GITHUB_DEFAULT_BRANCH", "main")
    if not repository or "/" not in repository:
        raise PromotionError("GITHUB_REPOSITORY is required for trusted source refresh")
    if os.environ.get("GITHUB_EVENT_NAME") != "workflow_dispatch":
        raise PromotionError("owned source refresh is available only through its trusted workflow_dispatch")
    if os.environ.get("GITHUB_REF") != f"refs/heads/{default_branch}":
        raise PromotionError("owned source refresh requires the workflow file from the trusted default branch")
    if not args.state_root or not args.datapan_cli:
        raise PromotionError("owned source refresh requires durable processor state and the pinned Datapan CLI")
    if args.prepare_only:
        raise PromotionError("owned source refresh cannot run in prepare-only mode")

    pr_number = args.refresh_pr_number
    predecessor_head = str(args.expected_predecessor_head_sha or "")
    predecessor_body = str(args.expected_predecessor_body_sha256 or "")
    predecessor_manifest = str(args.expected_predecessor_manifest_sha256 or "")
    target_main = str(args.target_main_sha or "")
    if isinstance(pr_number, bool) or not isinstance(pr_number, int) or pr_number < 1:
        raise PromotionError("owned source refresh requires a positive predecessor PR number")
    if not re.fullmatch(r"[a-f0-9]{40}", predecessor_head):
        raise PromotionError("owned source refresh predecessor head must be a full immutable Git SHA")
    for digest, label in ((predecessor_body, "predecessor body"), (predecessor_manifest, "predecessor manifest")):
        if not re.fullmatch(r"[a-f0-9]{64}", digest):
            raise PromotionError(f"owned source refresh {label} must be a full SHA-256 digest")
    if not re.fullmatch(r"[a-f0-9]{40}", target_main):
        raise PromotionError("owned source refresh target main must be a full immutable Git SHA")
    processor_state_sha = str(getattr(args, "processor_state_sha", "") or "")
    if processor_state_sha and not re.fullmatch(r"[a-f0-9]{40}", processor_state_sha):
        raise PromotionError("processor state pin must be a full immutable Git commit SHA")
    processor_state_repo = getattr(args, "processor_state_repo", None)
    if processor_state_sha and processor_state_repo is None:
        raise PromotionError("processor state pin requires the read-only B checkout")
    checkout_sha = command(("git", "rev-parse", "HEAD"), root).stdout.strip()
    if checkout_sha != target_main:
        raise PromotionError("trusted source refresh checkout does not equal the requested target main")
    assert_remote_main_sha(root, target_main)

    helper = load_canonical_update_pr(root)
    journal, state_sha = load_promotion_journal_snapshot(root)
    if not isinstance(journal, Mapping) or not state_sha:
        raise PromotionError("owned source refresh requires an adopted durable promotion journal")
    predecessors = [
        row for row in journal.get("records", [])
        if isinstance(row, Mapping)
        and str(row.get("candidate", {}).get("repository", "")).casefold() == repository.casefold()
        and row.get("candidate", {}).get("source_id") == "data_go_kr"
        and row.get("candidate", {}).get("scope") == "aggregate_supported_catalog"
        and row.get("pr", {}).get("number") == pr_number
        and row.get("candidate", {}).get("head_sha") == predecessor_head
        and row.get("ownership", {}).get("body_sha256") == predecessor_body
        and row.get("candidate", {}).get("manifest_sha256") == predecessor_manifest
    ]
    if len(predecessors) != 1:
        raise PromotionError("source refresh request does not identify exactly one adopted predecessor receipt")
    predecessor = dict(predecessors[0])
    if predecessor.get("status") != "pending-review":
        raise PromotionError("source refresh predecessor is not an active pending-review PR")
    if int(predecessor.get("pr", {}).get("number", 0)) != pr_number:
        raise PromotionError("source refresh predecessor PR number changed")
    assert_predecessor_base_is_ancestor(
        root, str(predecessor.get("candidate", {}).get("base_sha", "")), target_main,
    )

    predecessor_ref = helper.revision_reference(predecessor)
    records = [row for row in journal.get("records", []) if isinstance(row, Mapping)]
    completed: list[Mapping[str, Any]] = []
    if isinstance(predecessor.get("superseded_by"), Mapping):
        successor = next((
            row for row in records
            if helper._reference_matches_receipt(predecessor["superseded_by"], row)
        ), None)
        if (
            successor is None
            or successor.get("refresh_target_main_sha") != target_main
            or not helper._reference_matches_receipt(successor.get("refresh_from", {}), predecessor)
            or successor.get("status") != "pending-review"
        ):
            raise PromotionError("source refresh predecessor is already superseded by a different successor")
        completed = [successor]
    intents = [
        row for row in records
        if row.get("status") == "prepared"
        and row.get("superseded_by") is None
        and isinstance(row.get("refresh_from"), Mapping)
        and helper._reference_matches_receipt(row["refresh_from"], predecessor)
    ]
    if len(intents) > 1:
        raise PromotionError("source refresh predecessor has multiple prepared successor intents")
    if completed:
        successor = completed[0]
        candidate = successor.get("candidate", {})
        if (
            candidate.get("generation_id") != predecessor["candidate"].get("generation_id")
            or candidate.get("registry_sha256") != predecessor["candidate"].get("registry_sha256")
            or candidate.get("composition_receipt_sha256") != predecessor["candidate"].get("composition_receipt_sha256")
            or candidate.get("base_sha") != target_main
        ):
            raise PromotionError("completed source refresh did not preserve the B generation/payload on target main")
        observed = gh_pr_readback(root, repository, pr_number)
        validate_exact_open_pr(successor, observed, helper)
        print(json.dumps({
            "status": "already-refreshed", "pr_number": pr_number,
            "generation_id": candidate["generation_id"], "candidate_sha": candidate["head_sha"],
            "target_main_sha": target_main,
        }, sort_keys=True))
        return

    successor_head_sha = None
    if intents:
        intent = intents[0]
        candidate = intent.get("candidate", {})
        if (
            intent.get("refresh_target_main_sha") != target_main
            or candidate.get("base_sha") != target_main
            or candidate.get("generation_id") != predecessor["candidate"].get("generation_id")
            or candidate.get("registry_sha256") != predecessor["candidate"].get("registry_sha256")
            or candidate.get("composition_receipt_sha256") != predecessor["candidate"].get("composition_receipt_sha256")
        ):
            raise PromotionError("prepared source refresh intent conflicts with the requested target or B payload")
        successor_head_sha = str(candidate.get("head_sha", ""))
        if not re.fullmatch(r"[a-f0-9]{40}", successor_head_sha):
            raise PromotionError("prepared source refresh intent has no exact candidate head")
        observed = gh_pr_readback(root, repository, pr_number)
        phase = refresh_pr_phase(intent, predecessor, observed, helper)
        if phase != "before-push":
            assert_remote_main_sha(root, target_main)
    else:
        if predecessor.get("superseded_by") is not None:
            raise PromotionError("source refresh predecessor was superseded without its exact successor receipt")
        observed = gh_pr_readback(root, repository, pr_number)
        validate_exact_open_pr(predecessor, observed, helper)

    if processor_state_sha:
        checkout = pathlib.Path(processor_state_repo)
        if not checkout.is_absolute():
            checkout = root / checkout
        with tempfile.TemporaryDirectory(prefix="canonical-source-refresh-state-pin-") as raw_export:
            state_root, pin_provenance = export_processor_state_pin(
                checkout,
                processor_state_sha,
                repository,
                pathlib.Path(raw_export) / "state-root",
                command_cwd=root,
            )
            original_state_root = args.state_root
            args.state_root = state_root
            try:
                prepare_source_refresh_candidate(
                    args, root, repository, target_main, predecessor, state_sha,
                    successor_head_sha, state_root, pin_provenance, default_branch,
                    helper, journal,
                )
            finally:
                args.state_root = original_state_root
        return

    state_root = args.state_root.resolve()
    prepare_source_refresh_candidate(
        args, root, repository, target_main, predecessor, state_sha,
        successor_head_sha, state_root, None, default_branch, helper, journal,
    )


def prepare_source_refresh_candidate(
    args: argparse.Namespace,
    root: pathlib.Path,
    repository: str,
    target_main: str,
    predecessor: Mapping[str, Any],
    state_sha: str,
    successor_head_sha: str | None,
    state_root: pathlib.Path,
    pin_provenance: Mapping[str, str] | None,
    default_branch: str,
    helper: Any,
    journal: Mapping[str, Any],
) -> None:
    """Validate one exact B checkpoint and carry its selected state root into preparation."""
    schema_path = root / "schemas/datapan.upstream-catalogue-checkpoint.v1.schema.json"
    candidates, blocked = list_recoverable_processor_checkpoints(state_root, schema_path, journal)
    pr_number = int(predecessor.get("pr", {}).get("number", 0))
    generation_id = str(predecessor["candidate"].get("generation_id", ""))
    matching_checkpoints = [row for row in candidates if row.get("generation_id") == generation_id]
    if len(matching_checkpoints) != 1:
        reason = next((row["reason"] for row in blocked if row.get("generation_id") == generation_id), "missing_or_nonready_generation")
        raise PromotionError(f"predecessor B generation is not an exact recoverable ready checkpoint: {reason}")
    checkpoint = matching_checkpoints[0]
    if (
        checkpoint.get("source_id") != "data_go_kr"
        or checkpoint.get("source_scope") != "aggregate_supported_catalog"
        or checkpoint.get("generation_id") != generation_id
    ):
        raise PromotionError("durable B checkpoint differs from the predecessor's admitted source identity")
    run_id, attempt, _name = processor_attempt_from_locator(checkpoint)
    run = validate_trusted_processor_run(
        processor_run_api(root, repository, run_id, attempt), repository=repository,
        run_id=run_id, attempt=attempt, default_branch=default_branch,
    )
    artifact_id = str(checkpoint.get("output_artifact", {}).get("artifact_id", ""))
    artifact = processor_artifact_api(root, repository, run_id, artifact_id)
    if artifact is None:
        raise PromotionError("predecessor B artifact is no longer available")
    artifact = validate_processor_artifact_metadata(artifact, checkpoint, run, repository=repository)
    bundle_dir = download_processor_artifact(
        root, repository, artifact,
        root / ".datapan" / f"source-refresh-{pr_number}-{generation_id[:12]}" / "bundle",
    )
    composition_schema = load_object(root / "schemas/datapan.catalogue-composition-receipt.v1.schema.json")
    canonical_context = authenticated_current_canonical_context(root, target_main)
    bundle = validate_processor_bundle(
        checkpoint, bundle_dir, composition_schema, helper, root=root,
        producer_head_sha=str(run["head_sha"]),
        canonical_context=canonical_context,
        allow_terminal_noop=False,
        defer_seoul_declaration=True,
    )
    verify_processor_input_compatibility(
        root, checkpoint, str(run["head_sha"]), target_main,
        composition_receipt=bundle.get("composition_receipt"),
    )
    if checkpoint.get("generation_inputs", {}).get("same_observation_derivation") is not None:
        raise PromotionError("same-observation derivation cannot be combined with a trusted source-refresh predecessor")
    if bundle.get("status") in {"ready", "no-change"}:
        validate_processor_seoul_bundle(
            checkpoint, bundle_dir, bundle, root=root,
            canonical_context=canonical_context,
            allow_terminal_noop=False,
        )
    if (
        bundle.get("generation_id", checkpoint.get("generation_id")) != generation_id
        or bundle.get("registry_sha256") != predecessor["candidate"].get("registry_sha256")
        or bundle.get("composition_receipt_sha256") != predecessor["candidate"].get("composition_receipt_sha256")
    ):
        raise PromotionError("predecessor B bundle does not reproduce the exact reviewed generation and payload")

    if pin_provenance is not None:
        checkpoint_path = state_root / "sources/data_go_kr/generations" / f"{generation_id}.json"
        if checkpoint_path.is_symlink() or not checkpoint_path.is_file():
            raise PromotionError("selected processor state pin checkpoint is missing or unsafe")
        locator = checkpoint.get("output_artifact", {})
        print(json.dumps({
            "event": "processor_state_pin_selected",
            **dict(pin_provenance),
            "checkpoint_sha256": file_sha256(checkpoint_path),
            "generation_id": generation_id,
            "processor_run_id": run_id,
            "run_attempt": attempt,
            "artifact_id": artifact_id,
            "artifact_expires_at": locator.get("expires_at"),
            "bundle_manifest_sha256": locator.get("bundle_manifest_sha256"),
        }, sort_keys=True), flush=True)

    args.source_refresh_predecessor = predecessor
    args.source_refresh_target_main_sha = target_main
    args.source_refresh_expected_state_sha = state_sha
    args.source_refresh_successor_head_sha = successor_head_sha
    args.workflow_run_id = run_id
    args.workflow_run_attempt = attempt
    args.workflow_run_head_sha = str(run["head_sha"])
    args.processor_artifact_id = artifact_id
    args.bundle_dir = bundle_dir
    args._authenticated_canonical_context = canonical_context
    execute_candidate_preparation(args, root)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=("recover-ready", "reconcile-prs", "reconcile-publication", "refresh-owned-source"), default="recover-ready")
    parser.add_argument("--repository-root", type=pathlib.Path, default=pathlib.Path("."))
    parser.add_argument("--state-root", type=pathlib.Path)
    parser.add_argument("--bundle-dir", type=pathlib.Path)
    parser.add_argument("--datapan-cli", type=pathlib.Path)
    parser.add_argument("--workflow-run-id")
    parser.add_argument("--workflow-run-attempt")
    parser.add_argument("--workflow-run-head-sha")
    parser.add_argument("--processor-artifact-id")
    parser.add_argument("--event-run-id")
    parser.add_argument("--event-run-attempt")
    parser.add_argument("--event-head-sha")
    parser.add_argument("--publication-receipt", type=pathlib.Path)
    parser.add_argument("--refresh-pr-number", type=int)
    parser.add_argument("--expected-predecessor-head-sha")
    parser.add_argument("--expected-predecessor-body-sha256")
    parser.add_argument("--expected-predecessor-manifest-sha256")
    parser.add_argument("--target-main-sha")
    parser.add_argument("--processor-state-sha")
    parser.add_argument("--processor-state-repo", type=pathlib.Path)
    parser.add_argument("--evaluator-source-sha", help="Exact trusted C workflow checkout SHA captured before candidate work")
    parser.add_argument("--terminal-outcome-output", type=pathlib.Path, help="Exclusive attempt-specific terminal outcome JSON path under .datapan/ci/canonical-update-promotion-outcomes")
    parser.add_argument("--prepare-only", action="store_true", help="generate and validate a local candidate commit without uploading LFS, creating issues/PRs, or writing promotion state")
    args = parser.parse_args()
    root = args.repository_root.resolve()
    outcome_recorder: TerminalOutcomeRecorder | None = None
    try:
        repo = os.environ.get("GITHUB_REPOSITORY", "")
        if not repo or "/" not in repo:
            raise PromotionError("GITHUB_REPOSITORY is required")
        if args.terminal_outcome_output is not None:
            evaluator_source_sha = args.evaluator_source_sha or os.environ.get("C_EVALUATOR_SOURCE_SHA", "")
            outcome_recorder = TerminalOutcomeRecorder(
                root,
                args.terminal_outcome_output,
                repository=repo,
                mode=args.mode,
                evaluator_source_sha=evaluator_source_sha,
                run_id=os.environ.get("GITHUB_RUN_ID", ""),
                run_attempt=os.environ.get("GITHUB_RUN_ATTEMPT", ""),
                event_name=os.environ.get("GITHUB_EVENT_NAME", "local"),
            )
            if not outcome_recorder.source_matches:
                raise PromotionError("terminal_outcome_evaluator_source_mismatch")
        if args.processor_state_sha and args.mode != "refresh-owned-source":
            raise PromotionError("processor state pins are available only for explicit owned source refresh")
        if args.mode == "recover-ready":
            required = (args.state_root, args.datapan_cli)
            if any(value is None for value in required):
                raise PromotionError("ready recovery requires durable processor state and the pinned Datapan CLI")
            outcome = recover_ready_processor_candidate(args, root)
        elif args.mode == "refresh-owned-source":
            run_source_refresh(args, root)
            outcome = terminal_mode_result(args.mode)
        elif args.mode == "reconcile-prs":
            reconcile_open_promotions(root, prepare_only=args.prepare_only)
            outcome = terminal_mode_result(args.mode)
        else:
            if args.prepare_only:
                raise PromotionError("--prepare-only cannot be combined with reconcile-publication")
            if args.publication_receipt is None:
                raise PromotionError("reconcile-publication mode requires the downloaded immutable #592 receipt")
            reconcile_publication(root, args.publication_receipt.resolve())
            outcome = terminal_mode_result(args.mode)
        if outcome_recorder is not None:
            outcome_recorder.finish(outcome)
        return 0
    except Exception as exc:  # noqa: BLE001
        if outcome_recorder is not None:
            try:
                outcome_recorder.fail(terminal_execution_failure_code(exc))
            except Exception:
                print("FAIL canonical update promotion terminal outcome could not be sealed", file=sys.stderr)
        print(f"FAIL canonical update promotion: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
