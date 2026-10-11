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
import importlib.machinery
import importlib.util
import io
import json
import math
import marshal
import os
import pathlib
import re
import selectors
import shutil
import shlex
import signal
import stat
import subprocess
import sys
import tarfile
import tempfile
import time
import types
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from collections.abc import Callable, Mapping, Sequence
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
    "scripts/upstream_catalogue_cache_source.py",
    "scripts/seoul_oa109_operation_declaration.py",
    "scripts/generate-seoul-oa109-subject-snapshot.py",
    "scripts/compose-upstream-catalogue-candidate.py",
    "schemas/datapan.upstream-catalogue-checkpoint.v1.schema.json",
    "schemas/datapan.catalogue-composition-receipt.v1.schema.json",
    *PROCESSOR_COMPOSITION_INPUTS.values(),
)
PROCESSOR_DERIVATION_COMPATIBILITY_FILE = "scripts/upstream_catalogue_derivation.py"
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
    "upstream-catalogue-original-candidate.registry.json",
    "upstream-catalogue-cache-source-context.json",
    "upstream-catalogue-enrichment-evidence.json",
    "upstream-catalogue-processing-result.json",
)
HISTORICAL_RETAINED_PROCESSOR_FILES = (
    "composed-candidate.registry.json",
    "ready-scope.registry.json",
    "semantic-diff.json",
    "regeneration-queue.json",
    "quarantine.json",
    "composition-receipt.json",
    "upstream-catalogue-enrichment-evidence.json",
    "upstream-catalogue-processing-result.json",
)
RETAINED_PROCESSOR_RESULT_ONLY_FILES = (
    "upstream-catalogue-processing-result.json",
)
RETAINED_PROCESSOR_CHECKPOINT_MEMBER = "upstream-catalogue-checkpoint-receipt.json"
MODERN_RETAINED_PROCESSOR_PROTOCOL = "modern-ten-output-v1"
HISTORICAL_RETAINED_PROCESSOR_PROTOCOL = "historical-eight-output-v1"
HISTORICAL_RETAINED_PROCESSOR_CURRENT_REASON = (
    "historical_eight_output_protocol_not_current_eligible"
)
# This is the exact closed producer/source format of the retained 01148419 B
# packet used by the completeness report.  Matching file absence or an
# eight-member inventory is never enough to select the historical reader.
# The same immutable bytes may appear at another authenticated B head, but an
# unknown source closure must remain unavailable until separately reviewed.
HISTORICAL_RETAINED_PROCESSOR_SOURCE_IDENTITIES = {
    "policy/source-refresh.json": (1127, "61dfb817f743085918c9f613fcd4a2dcf0d2f3de9df31b005cae1cd0250d5cfc"),
    "data/provider-index.json": (26315, "b235d6f053bd8ec1f87d6a889a07912dfde1a944353eb640da8713fdf640e4d6"),
    "scripts/generate-batch-link-detail-registry-patches.py": (10251, "9cb122dd6f20bee3faf36359bb22d66b165cb2ccc43a13ec0408796108910983"),
    "scripts/process-upstream-catalogue-candidate.py": (111277, "418be4c660fc59beae4ad8fa3c04cab5385a6c174103ea2af268cb65c35915b2"),
    "scripts/compose-upstream-catalogue-candidate.py": (70733, "2e5ce37eb68002a28c5d313ee9b274b2bdfabf845211d384ea804d6685787e08"),
    ".github/workflows/upstream-catalogue-process.yml": (83097, "a0a5de9e921e11d8ea49516507a662164e67caf436ffb6d2e193d11fde4d0ff2"),
    "schemas/datapan.upstream-catalogue-checkpoint.v1.schema.json": (9959, "2f276ab822ea81f7caa6854bbca830b06c578507044e646d902758b556dc0639"),
    "schemas/datapan.specs.v1.schema.json": (3371, "ca5b95521fe4772b73dbff3bac915d475098270691cd408fb5e615a16d9c0cd9"),
    "schemas/datapan.provider-index.v1.schema.json": (3167, "00258077f9ca3421f755e4cad7f39d1a8e89b4fe072d6f9c77ae82ebb7993b05"),
    "schemas/datapan.catalog-diff.v1.schema.json": (4278, "2cc4dd60b4455276a34f9751b6ee92457d4a8734e070578e608550222499959f"),
    "schemas/datapan.upstream-refresh-evidence.v1.schema.json": (3866, "81eb66ec3bbcd1a0687b5b8cc0cace6936a8b4fef6414a760bd50f9c8d23b917"),
    "schemas/datapan.catalogue-enrichment-evidence.v1.schema.json": (4401, "fd9f7ef1d5ea77617191a3548ecb485d81775247aa64d358126675a8dc2462c7"),
    "schemas/datapan.catalogue-composition-receipt.v1.schema.json": (3740, "00e13f478c7902bdd457f8b27916237b89de4f7c491b5d85915d80b9d507d1b8"),
}


class PromotionError(RuntimeError):
    pass


class ProcessorCandidateError(PromotionError):
    """One retained B generation is unusable; later durable generations may proceed."""


class ProcessorArtifactUnavailable(ProcessorCandidateError):
    pass


_RETAINED_REPORT_CAPABILITY = object()


class RetainedProcessorReportContract:
    """Opaque protocol decision for one exact retained B report packet."""

    __slots__ = (
        "_protocol", "_expected_members", "_generation_id", "_checkpoint_sha256",
        "_producer_head_sha", "_run_id", "_run_attempt", "_artifact_id",
        "_archive_sha256", "_archive_bytes", "_source_reader", "_cache_source_helper",
        "_cache_source_contract", "_repository", "_default_branch",
    )

    def __init__(
        self, token: object, *, protocol: str, expected_members: Sequence[str],
        checkpoint: Mapping[str, Any], producer_head_sha: str, run_id: str,
        run_attempt: int, artifact_id: str, archive_sha256: str, archive_bytes: int,
        source_reader: Any, cache_source_helper: Any, repository: str,
        default_branch: str,
        cache_source_contract: Any | None,
    ) -> None:
        if token is not _RETAINED_REPORT_CAPABILITY:
            raise PromotionError("retained processor report contract cannot be constructed directly")
        self._protocol = protocol
        self._expected_members = tuple(expected_members)
        self._generation_id = str(checkpoint.get("generation_id") or "")
        self._checkpoint_sha256 = str(checkpoint.get("checkpoint_sha256") or "")
        self._producer_head_sha = producer_head_sha
        self._run_id = run_id
        self._run_attempt = run_attempt
        self._artifact_id = artifact_id
        self._archive_sha256 = archive_sha256
        self._archive_bytes = archive_bytes
        self._source_reader = source_reader
        self._cache_source_helper = cache_source_helper
        self._cache_source_contract = cache_source_contract
        self._repository = repository
        self._default_branch = default_branch

    @property
    def protocol(self) -> str:
        return self._protocol

    @property
    def expected_members(self) -> tuple[str, ...]:
        return self._expected_members

    @property
    def cache_source_contract(self) -> Any:
        if (
            self._protocol != MODERN_RETAINED_PROCESSOR_PROTOCOL
            or type(self._cache_source_contract)
            is not self._cache_source_helper.AuthenticatedCacheContract
        ):
            raise PromotionError("retained processor report contract has no modern cache capability")
        return self._cache_source_contract


class RetainedProcessorReportBundle:
    """Closed report facts; this value carries no live-C/cache capability."""

    __slots__ = ("_protocol", "_facts")

    def __init__(self, token: object, *, protocol: str, facts: Mapping[str, Any]) -> None:
        if token is not _RETAINED_REPORT_CAPABILITY:
            raise PromotionError("retained processor report bundle cannot be constructed directly")
        self._protocol = protocol
        self._facts = copy.deepcopy(dict(facts))

    @property
    def protocol(self) -> str:
        return self._protocol

    def as_report_facts(self) -> dict[str, Any]:
        return copy.deepcopy(self._facts)


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
    loader = _NoBytecodeSourceLoader(name, str(path))
    spec = importlib.util.spec_from_file_location(name, path, loader=loader)
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


def verify_processor_checkpoint(
    value: dict[str, Any],
    schema_path: pathlib.Path,
    *,
    eligibility_module: types.ModuleType | None = None,
    schema_document: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    import jsonschema

    if value.get("schema_version") != PROCESSOR_SCHEMA:
        raise PromotionError("processor state uses an unsupported checkpoint schema")
    if eligibility_module is not None and type(eligibility_module) is not types.ModuleType:
        raise PromotionError("processor checkpoint eligibility helper is not an exact module")
    if schema_document is not None and type(schema_document) is not dict:
        raise PromotionError("processor checkpoint CONTROL schema is not an exact JSON object")
    if schema_document is not None and eligibility_module is None:
        raise PromotionError("processor checkpoint CONTROL schema has no authenticated eligibility module")
    schema = schema_document if schema_document is not None else load_object(schema_path)
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
    source_epoch = generation_inputs.get("source_epoch")
    if source_epoch is None:
        expected_generation_id = hashlib.sha256(canonical_json(generation_inputs)).hexdigest()
    else:
        if eligibility_module is None:
            # Ordinary Runner checkpoint reads use the helper beside the
            # current Source schema. A replay supplies its exact authenticated
            # CONTROL module above and therefore never enters this path.
            helper_path = schema_path.resolve().parent.parent / "scripts/upstream_catalogue_source_eligibility.py"
            try:
                eligibility_module = load_module(
                    helper_path, "trusted_processor_checkpoint_source_eligibility",
                )
            except Exception as exc:
                raise PromotionError("source-epoch checkpoint eligibility helper is unavailable") from exc
        if type(eligibility_module) is not types.ModuleType:
            raise PromotionError("source-epoch checkpoint has no authenticated eligibility module")
        validate_epoch = getattr(eligibility_module, "validate_source_epoch", None)
        validate_contract = getattr(eligibility_module, "validate_target_contract", None)
        bind_generation = getattr(eligibility_module, "bind_source_epoch_generation", None)
        if not callable(validate_epoch) or not callable(validate_contract) or not callable(bind_generation):
            raise PromotionError("source-epoch checkpoint CONTROL identity helpers are unavailable")
        try:
            validated_epoch = validate_epoch(source_epoch)
            validated_contract = validate_contract(generation_inputs.get("source_target_contract"))
            if validated_epoch["target_contract_sha256"] != validated_contract["contract_sha256"]:
                raise ValueError("source_epoch_target_contract_mismatch")
            expected_generation_id, bound_inputs = bind_generation(generation_inputs)
        except Exception as exc:
            raise PromotionError("processor checkpoint source-epoch identity is invalid") from exc
        if (
            type(bound_inputs) is not dict
            or canonical_json(bound_inputs) != canonical_json(generation_inputs)
        ):
            raise PromotionError("processor checkpoint source-epoch inputs are not canonically bound")
    if value.get("generation_id") != expected_generation_id:
        raise PromotionError("processor checkpoint generation id does not bind its immutable inputs")
    return value


def _source_eligibility_state_snapshot(
    state_root: pathlib.Path,
    repository: str,
    generation_id: str,
    schema_path: pathlib.Path,
    *,
    eligibility_module: types.ModuleType | None = None,
) -> dict[str, Any]:
    """Bind one eligible generation to the exact committed processor state."""
    if not re.fullmatch(r"[a-f0-9]{64}", generation_id):
        raise PromotionError("source eligibility generation identity is invalid")
    state_root = state_root.resolve()
    state_repository = next(
        (candidate for candidate in (state_root, *state_root.parents)
         if (candidate / ".git").exists() and not (candidate / ".git").is_symlink()),
        None,
    )
    if state_repository is None:
        raise PromotionError("source eligibility state checkout is not a Git repository")
    try:
        relative_root = state_root.relative_to(state_repository)
    except ValueError as exc:
        raise PromotionError("source eligibility state root escaped its checkout") from exc
    if pathlib.PurePosixPath(relative_root.as_posix()) != PROCESSOR_STATE_ROOT:
        raise PromotionError("source eligibility state root is not the owned processor state path")
    origin_url = command(("git", "config", "--get", "remote.origin.url"), state_repository).stdout.strip()
    if processor_state_repository_from_remote(origin_url).casefold() != repository.casefold():
        raise PromotionError("source eligibility state checkout names another repository")
    if command(("git", "status", "--porcelain=v1", "--untracked-files=all"), state_repository).stdout:
        raise PromotionError("source eligibility state checkout is dirty")
    state_commit = command(("git", "rev-parse", "HEAD"), state_repository).stdout.strip()
    if not re.fullmatch(r"[a-f0-9]{40}", state_commit):
        raise PromotionError("source eligibility state commit is invalid")

    index_relative = PROCESSOR_STATE_ROOT / "sources/data_go_kr/index.json"
    checkpoint_relative = PROCESSOR_STATE_ROOT / "sources/data_go_kr/generations" / f"{generation_id}.json"
    file_limits = {
        index_relative: 2 * 1024 * 1024,
        checkpoint_relative: 256 * 1024,
    }

    def object_spec(relative: pathlib.PurePosixPath) -> str:
        object_spec = f"{state_commit}:{relative.as_posix()}"
        object_type = command(("git", "cat-file", "-t", object_spec), state_repository).stdout.strip()
        if object_type != "blob":
            raise PromotionError("source eligibility state object is not a Git blob")
        size_text = command(("git", "cat-file", "-s", object_spec), state_repository).stdout.strip()
        if not re.fullmatch(r"[0-9]{1,10}", size_text):
            raise PromotionError("source eligibility state object size is invalid")
        if not 0 < int(size_text) <= file_limits[relative]:
            raise PromotionError("source eligibility state object exceeds its byte limit")
        return object_spec

    def read_committed(relative: pathlib.PurePosixPath) -> bytes:
        spec = object_spec(relative)
        result = subprocess.run(
            ("git", "cat-file", "blob", spec), cwd=state_repository,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False,
        )
        if result.returncode != 0 or len(result.stdout) > file_limits[relative]:
            raise PromotionError("source eligibility state object is unavailable from the pinned commit")
        return result.stdout

    def read_working(relative: pathlib.PurePosixPath) -> bytes:
        path = state_repository / pathlib.Path(*relative.parts)
        try:
            before = path.lstat()
        except OSError as exc:
            raise PromotionError("source eligibility state files are missing or unsafe") from exc
        if not stat.S_ISREG(before.st_mode) or before.st_size > file_limits[relative]:
            raise PromotionError("source eligibility state files are missing or unsafe")
        try:
            descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
            with os.fdopen(descriptor, "rb") as handle:
                opened = os.fstat(handle.fileno())
                if (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino):
                    raise PromotionError("source eligibility state file changed while opening")
                payload = handle.read(file_limits[relative] + 1)
                if len(payload) > file_limits[relative]:
                    raise PromotionError("source eligibility state file exceeds its byte limit")
                if handle.read(1):
                    raise PromotionError("source eligibility state file changed while reading")
        except OSError as exc:
            raise PromotionError("source eligibility state files are missing or unsafe") from exc
        return payload

    index_path = state_repository / pathlib.Path(*index_relative.parts)
    checkpoint_path = state_repository / pathlib.Path(*checkpoint_relative.parts)
    index_bytes = read_working(index_relative)
    checkpoint_bytes = read_working(checkpoint_relative)
    if read_committed(index_relative) != index_bytes or read_committed(checkpoint_relative) != checkpoint_bytes:
        raise PromotionError("source eligibility working state differs from its pinned commit")
    try:
        index = json.loads(index_bytes)
        checkpoint_value = json.loads(checkpoint_bytes)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PromotionError("source eligibility state JSON is invalid") from exc
    if not isinstance(index, Mapping) or index.get("schema_version") != PROCESSOR_SCHEMA:
        raise PromotionError("source eligibility state index is invalid")
    rows = index.get("generations")
    matches = [
        row for row in rows if isinstance(row, Mapping)
        and row.get("generation_id") == generation_id
        and row.get("checkpoint") == f"{generation_id}.json"
    ] if isinstance(rows, list) else []
    if len(matches) != 1 or not isinstance(checkpoint_value, dict):
        raise PromotionError("source eligibility generation is not uniquely indexed")
    checkpoint = verify_processor_checkpoint(
        checkpoint_value, schema_path, eligibility_module=eligibility_module,
    )
    if checkpoint.get("generation_id") != generation_id:
        raise PromotionError("source eligibility checkpoint identity differs from its index")

    if (
        command(("git", "rev-parse", "HEAD"), state_repository).stdout.strip() != state_commit
        or read_working(index_relative) != index_bytes
        or read_working(checkpoint_relative) != checkpoint_bytes
        or read_committed(index_relative) != index_bytes
        or read_committed(checkpoint_relative) != checkpoint_bytes
    ):
        raise PromotionError("source eligibility state changed during snapshot")
    return {
        "state_repository": state_repository,
        "state_commit": state_commit,
        "index_relative": index_relative.as_posix(),
        "checkpoint_relative": checkpoint_relative.as_posix(),
        "index_bytes": index_bytes,
        "checkpoint_bytes": checkpoint_bytes,
        "index": dict(index),
        "checkpoint": checkpoint,
    }


def _source_eligibility_selection_state_revision(
    state_root: pathlib.Path,
    repository: str,
) -> str | None:
    """Pin the clean processor-state revision before selecting a generation."""
    state_root = state_root.resolve()
    marker_path = state_root / "state-root.json"
    try:
        marker_stat = marker_path.lstat()
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise PromotionError("source eligibility state marker is unavailable") from exc
    if not stat.S_ISREG(marker_stat.st_mode) or marker_stat.st_size > 8192:
        raise PromotionError("source eligibility state marker is unsafe")
    try:
        descriptor = os.open(marker_path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        with os.fdopen(descriptor, "rb") as handle:
            opened = os.fstat(handle.fileno())
            if (opened.st_dev, opened.st_ino) != (marker_stat.st_dev, marker_stat.st_ino):
                raise PromotionError("source eligibility state marker changed while opening")
            marker_bytes = handle.read(8193)
            if len(marker_bytes) > 8192 or handle.read(1):
                raise PromotionError("source eligibility state marker changed while reading")
        marker = json.loads(marker_bytes)
    except PromotionError:
        raise
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PromotionError("source eligibility state marker is invalid") from exc
    if marker != {
        "schema_version": "datapan.upstream-catalogue-state-root.v1",
        "repository": repository,
        "source_id": "data_go_kr",
        "state_root": PROCESSOR_STATE_ROOT.as_posix(),
    }:
        raise PromotionError("source eligibility state marker identity is invalid")

    state_repository = next(
        (candidate for candidate in (state_root, *state_root.parents)
         if (candidate / ".git").exists() and not (candidate / ".git").is_symlink()),
        None,
    )
    if state_repository is None:
        raise PromotionError("source eligibility state checkout is not a Git repository")
    try:
        relative_root = state_root.relative_to(state_repository)
    except ValueError as exc:
        raise PromotionError("source eligibility state root escaped its checkout") from exc
    if pathlib.PurePosixPath(relative_root.as_posix()) != PROCESSOR_STATE_ROOT:
        raise PromotionError("source eligibility state root is not the owned processor state path")
    origin_url = command(("git", "config", "--get", "remote.origin.url"), state_repository).stdout.strip()
    if processor_state_repository_from_remote(origin_url).casefold() != repository.casefold():
        raise PromotionError("source eligibility state checkout names another repository")
    if command(("git", "status", "--porcelain=v1", "--untracked-files=all"), state_repository).stdout:
        raise PromotionError("source eligibility state checkout is dirty")
    state_commit = command(("git", "rev-parse", "HEAD"), state_repository).stdout.strip()
    if not re.fullmatch(r"[a-f0-9]{40}", state_commit):
        raise PromotionError("source eligibility state commit is invalid")
    if (
        command(("git", "rev-parse", "HEAD"), state_repository).stdout.strip() != state_commit
        or command(("git", "status", "--porcelain=v1", "--untracked-files=all"), state_repository).stdout
    ):
        raise PromotionError("source eligibility selection state changed while pinning")
    return state_commit


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


def authenticated_processor_compatibility_paths(
    generation_inputs: Mapping[str, Any],
    *,
    root: pathlib.Path,
    producer_file: Callable[[str], bytes],
) -> tuple[str, ...]:
    """Return the closed compatibility projection after authenticating derived source."""
    if not isinstance(generation_inputs, Mapping):
        raise PromotionError("processor checkpoint lacks generation input provenance")
    paths = list(PROCESSOR_COMPATIBILITY_FILES)
    derivation_value = generation_inputs.get("same_observation_derivation")
    if derivation_value is None:
        return tuple(dict.fromkeys(paths))
    try:
        derivation_helper = load_module(
            root / PROCESSOR_DERIVATION_COMPATIBILITY_FILE,
            "processor_compatibility_same_observation_derivation",
        )
        derivation = derivation_helper.validate_derivation_envelope(derivation_value)
    except Exception as exc:
        raise PromotionError("processor same-observation derivation envelope is invalid") from exc
    try:
        material = {
            "processor_script_sha256": hashlib.sha256(
                producer_file("scripts/process-upstream-catalogue-candidate.py")
            ).hexdigest(),
            "collector_handoff_helper_sha256": hashlib.sha256(
                producer_file("scripts/upstream_catalogue_handoff.py")
            ).hexdigest(),
            "same_observation_derivation_helper_sha256": hashlib.sha256(
                producer_file(PROCESSOR_DERIVATION_COMPATIBILITY_FILE)
            ).hexdigest(),
            "cache_source_helper_sha256": hashlib.sha256(
                producer_file("scripts/upstream_catalogue_cache_source.py")
            ).hexdigest(),
        }
    except Exception as exc:
        raise PromotionError("processor same-observation producer source is unavailable") from exc
    expected_revision = hashlib.sha256(canonical_json(material)).hexdigest()
    if derivation.get("derivation_processor_revision_sha256") != expected_revision:
        raise PromotionError("processor same-observation revision differs from producer source")
    paths.append(PROCESSOR_DERIVATION_COMPATIBILITY_FILE)
    return tuple(dict.fromkeys(paths))


def verify_processor_input_compatibility(
    root: pathlib.Path,
    checkpoint: Mapping[str, Any],
    processor_head_sha: str,
    current_head_sha: str,
    composition_receipt: Mapping[str, Any] | None = None,
    *, cache_source_helper: Any | None = None,
    cache_source_contract: Any | None = None,
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
    if (
        cache_source_helper is None
        or type(cache_source_contract) is not cache_source_helper.AuthenticatedCacheContract
        or cache_source_contract.producer_head_sha != processor_head_sha
    ):
        raise PromotionError("processor cache source contract is not bound to its source commit")
    if cache_source_contract.profile != cache_source_helper.CACHE_SOURCE_PROFILE:
        raise PromotionError("processor cache source contract profile is unsupported")
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
    cache_source_path = "scripts/upstream_catalogue_cache_source.py"
    declaration_helper_path = "scripts/seoul_oa109_operation_declaration.py"
    snapshot_generator_path = "scripts/generate-seoul-oa109-subject-snapshot.py"
    declaration_path = "contracts/provider-operation-declarations/data-go-kr-15056854-oa-109-search-last-train-time.v1.json"
    historical_snapshot_path = "contracts/provider-operation-declarations/data-go-kr-15056854-historical-subject-0085.v1.json"
    processor_bytes = historical_bytes.get(processor_path)
    handoff_bytes = historical_bytes.get(handoff_path)
    cache_source_bytes = historical_bytes.get(cache_source_path)
    declaration_helper_bytes = historical_bytes.get(declaration_helper_path)
    snapshot_generator_bytes = historical_bytes.get(snapshot_generator_path)
    declaration_bytes = historical_bytes.get(declaration_path)
    historical_snapshot_bytes = historical_bytes.get(historical_snapshot_path)
    if any(value is None for value in (
        processor_bytes, handoff_bytes, cache_source_bytes, declaration_helper_bytes, snapshot_generator_bytes,
        declaration_bytes, historical_snapshot_bytes,
    )):
        raise PromotionError("processor source is missing a cache-source or operation declaration compatibility input")
    generator_material = {
        "processor_script_sha256": hashlib.sha256(processor_bytes).hexdigest(),
        "collector_handoff_helper_sha256": hashlib.sha256(handoff_bytes).hexdigest(),
        "cache_source_helper_sha256": hashlib.sha256(cache_source_bytes).hexdigest(),
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
        assert cache_source_bytes is not None
        derivation_material["cache_source_helper_sha256"] = hashlib.sha256(cache_source_bytes).hexdigest()
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
    if archive.exists() or archive.is_symlink():
        try:
            mode = archive.lstat().st_mode
        except OSError as exc:
            raise ProcessorCandidateError("processor archive path could not be inspected") from exc
        if not stat.S_ISREG(mode) or stat.S_ISLNK(mode):
            raise ProcessorCandidateError("processor archive path is not a regular private file")
        archive.unlink()
    try:
        with archive.open("xb") as handle:
            for offset in range(0, len(raw), 1024 * 1024):
                handle.write(raw[offset:offset + 1024 * 1024])
    except OSError as exc:
        raise ProcessorCandidateError("processor archive could not be retained as a private file") from exc
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
    replay_control: OriginalCReplayControlBinding | None = None,
    deadline_monotonic: float | None = None,
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
        if replay_control is not None:
            if deadline_monotonic is None:
                raise PromotionError("original C replay schema access has no inherited deadline")
            replay_control.require(deadline_monotonic=deadline_monotonic)
            enrichment_schema = replay_control.data(
                "enrichment_schema", deadline_monotonic=deadline_monotonic,
            )
        else:
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
            replay_control=replay_control,
            deadline_monotonic=deadline_monotonic,
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
    declaration_document: Mapping[str, Any] | None = None
    if replay_control is not None:
        if deadline_monotonic is None:
            raise PromotionError("original C replay helper access has no inherited deadline")
        composer = replay_control.module("composer", deadline_monotonic=deadline_monotonic)
        helper = replay_control.module("link_detail", deadline_monotonic=deadline_monotonic)
        declaration_document = replay_control.data(
            "seoul_operation_declaration", deadline_monotonic=deadline_monotonic,
        )
        if getattr(composer, "LINK_DETAIL_HELPERS", None) is not helper:
            raise PromotionError("original C replay composer changed its authenticated link-detail dependency")
    else:
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
        if replay_control is not None:
            provider_index = replay_control.data(
                "provider_index", deadline_monotonic=deadline_monotonic,
            )
        else:
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
            projection = composer.worker_contract_projection(
                dict(item), seoul_declaration_document=declaration_document,
            )
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
    process: subprocess.Popen[bytes] | None = None
    selector: selectors.BaseSelector | None = None
    stdout = None
    stderr = None
    try:
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

        stdout = process.stdout
        stderr = process.stderr
        if stdout is None or stderr is None:
            return None
        selector = selectors.DefaultSelector()
        digest = hashlib.sha256()
        counts = {"stdout": 0, "stderr": 0}
        streams = {
            stdout: ("stdout", MAX_PROCESSOR_COMPOSER_BYTES),
            stderr: ("stderr", MAX_PROCESSOR_COMPOSER_STDERR_BYTES),
        }
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
        try:
            if process is not None:
                if process.poll() is None:
                    _terminate_and_reap_processor_composer_read(process)
                else:
                    process.wait()
        finally:
            try:
                if selector is not None:
                    selector.close()
            finally:
                try:
                    if stdout is not None:
                        stdout.close()
                finally:
                    if stderr is not None:
                        stderr.close()


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
    replay_control: OriginalCReplayControlBinding | None = None,
    deadline_monotonic: float | None = None,
) -> None:
    """Revalidate a successful pinned declaration against the trusted source and C outputs."""
    declaration_document: Mapping[str, Any] | None = None
    if replay_control is not None:
        if deadline_monotonic is None:
            raise PromotionError("original C replay declaration access has no inherited deadline")
        composer = replay_control.module("composer", deadline_monotonic=deadline_monotonic)
        declaration = replay_control.module("seoul_declaration", deadline_monotonic=deadline_monotonic)
        link_detail = replay_control.module("link_detail", deadline_monotonic=deadline_monotonic)
        provider_index = replay_control.data("provider_index", deadline_monotonic=deadline_monotonic)
        schema = replay_control.data("specs_schema", deadline_monotonic=deadline_monotonic)
        historical_subject = replay_control.data(
            "historical_subject_declaration", deadline_monotonic=deadline_monotonic,
        )
        declaration_document = replay_control.data(
            "seoul_operation_declaration", deadline_monotonic=deadline_monotonic,
        )
        if (
            getattr(composer, "SEOUL_OPERATION_DECLARATION", None) is not declaration
            or getattr(composer, "LINK_DETAIL_HELPERS", None) is not link_detail
        ):
            raise PromotionError("original C replay declaration dependency identity changed")
        try:
            declaration_document = declaration.validate_declaration_document(declaration_document)
        except Exception as exc:
            raise PromotionError("original C replay declaration document is invalid") from exc
    else:
        try:
            composer = load_module(
                root / "scripts/compose-upstream-catalogue-candidate.py",
                "processor_catalogue_composer_for_declaration_validation",
            )
        except (OSError, PromotionError) as exc:
            raise PromotionError("trusted processor composer is unavailable for declaration validation") from exc
        declaration = getattr(composer, "SEOUL_OPERATION_DECLARATION", None)
        provider_index = None
        schema = None
        historical_subject = None
        link_detail = getattr(composer, "LINK_DETAIL_HELPERS", None)
        declaration_document = getattr(declaration, "DECLARATION", None)
    declaration_id = getattr(declaration, "DECLARATION_ID", None)
    if not isinstance(declaration_id, str) or not declaration_id:
        raise PromotionError("trusted Seoul operation declaration validator is unavailable")
    declaration_value = (
        declaration_document
        if replay_control is not None
        else declaration.DECLARATION
    )
    target_id = str(declaration_value["subject"]["portal_dataset_id"])
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
        historical_snapshot = declaration.load_historical_subject_snapshot(
            root,
            authenticated_snapshot=historical_subject,
            declaration_document=declaration_document,
        )
        pinned_source_row = copy.deepcopy(dict(historical_snapshot["subject_row"]))
        declaration.validate_subject_row(
            pinned_source_row, root=root,
            authenticated_snapshot=historical_snapshot,
            declaration_document=declaration_document,
        )
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
        declaration.validate_enriched_record(
            original_row, record, root=root,
            authenticated_snapshot=historical_snapshot,
            declaration_document=declaration_document,
        )
        generation_inputs = checkpoint.get("generation_inputs")
        candidate_sha256 = generation_inputs.get("candidate_sha256") if isinstance(generation_inputs, Mapping) else None
        if replay_control is None:
            provider_index = load_object(root / "data/provider-index.json")
            provider_index_sha256 = file_sha256(root / "data/provider-index.json")
            schema = load_object(root / "schemas/datapan.specs.v1.schema.json")
        else:
            provider_index_sha256 = _original_c_control_data_sha256(
                replay_control, "data/provider-index.json",
                deadline_monotonic=deadline_monotonic,
            )
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
            seoul_declaration_document=declaration_document,
            historical_subject_snapshot=historical_snapshot,
        )
        validated_record = validated.get(("data.go.kr", target_id))
        if not isinstance(validated_record, Mapping) or validated_record.get("_binding_error"):
            raise PromotionError("processor Seoul declaration semantic binding was rejected")
        declaration.validate_declared_operation(
            original_row, record_op, root=root,
            authenticated_snapshot=historical_snapshot,
            declaration_document=declaration_document,
        )
        link_detail.validate_link_metadata(
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
    replay_control: OriginalCReplayControlBinding | None = None,
    deadline_monotonic: float | None = None,
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
            replay_control=replay_control,
            deadline_monotonic=deadline_monotonic,
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



def authenticate_processor_cache_source_contract(
    root: pathlib.Path,
    checkpoint: Mapping[str, Any],
    *,
    repository: str,
    default_branch: str,
    native_reader: Any | None = None,
    archive_path: pathlib.Path | None = None,
) -> tuple[Any, Any]:
    """Mint one opaque cache-source contract from native B/source/archive reads."""
    helper = load_module(
        root / "scripts/upstream_catalogue_cache_source.py",
        f"promotion_upstream_catalogue_cache_source_{id(checkpoint)}",
    )
    try:
        reader = native_reader or helper.GitHubNativeReader(
            cwd=root,
            deadline_monotonic=time.monotonic() + helper.MAX_WHOLE_ATTEMPT_SECONDS,
        )
        if archive_path is None:
            contract = helper.authenticate_owner_contract(
                checkpoint,
                repository=repository,
                default_branch=default_branch,
                reader=reader,
            )
        else:
            contract = helper.authenticate_owner_contract_from_archive_path(
                checkpoint,
                repository=repository,
                default_branch=default_branch,
                reader=reader,
                archive_path=archive_path,
            )
    except Exception as exc:
        raise PromotionError("processor cache source producer contract is unauthenticated") from exc
    if type(contract) is not helper.AuthenticatedCacheContract:
        raise PromotionError("processor cache source producer contract has an invalid capability type")
    return helper, contract


def _retained_processor_checkpoint_identity(
    checkpoint: Mapping[str, Any],
) -> tuple[Mapping[str, Any], Mapping[str, Any]]:
    """Validate the immutable checkpoint seal before choosing an output protocol."""
    if checkpoint.get("schema_version") != PROCESSOR_SCHEMA:
        raise PromotionError("retained processor checkpoint schema is unsupported")
    claimed_checkpoint_sha256 = checkpoint.get("checkpoint_sha256")
    unsigned = copy.deepcopy(dict(checkpoint))
    unsigned.pop("checkpoint_sha256", None)
    if (
        not isinstance(claimed_checkpoint_sha256, str)
        or not re.fullmatch(r"[a-f0-9]{64}", claimed_checkpoint_sha256)
        or hashlib.sha256(canonical_json(unsigned)).hexdigest() != claimed_checkpoint_sha256
    ):
        raise PromotionError("retained processor checkpoint digest is invalid")
    generation_inputs = checkpoint.get("generation_inputs")
    if (
        not isinstance(generation_inputs, Mapping)
        or generation_inputs.get("source_id") != checkpoint.get("source_id")
        or generation_inputs.get("source_scope") != checkpoint.get("source_scope")
        or checkpoint.get("generation_id")
        != hashlib.sha256(canonical_json(generation_inputs)).hexdigest()
    ):
        raise PromotionError("retained processor generation identity is invalid")
    locator = checkpoint.get("output_artifact")
    if not isinstance(locator, Mapping):
        raise PromotionError("retained processor checkpoint has no output artifact locator")
    return generation_inputs, locator


def _retained_processor_native_identity(
    checkpoint: Mapping[str, Any],
    run: Mapping[str, Any],
    artifact_inventory: Mapping[str, Any],
    archive_bytes: bytes,
    *,
    repository: str,
    default_branch: str,
    cache_source_helper: Any,
    source_reader: Any,
    deadline_monotonic: float,
) -> tuple[str, str, int, str, str]:
    """Bind retained ZIP bytes to one native B attempt and bounded local Git reader."""
    if (
        type(source_reader) is not cache_source_helper.LocalGitSourceReader
        or source_reader.deadline != float(deadline_monotonic)
    ):
        raise PromotionError("retained processor report requires its exact bounded local Git reader")
    try:
        source_reader.check_deadline()
    except Exception as exc:
        raise PromotionError("retained processor report source deadline is unavailable") from exc
    locator = checkpoint.get("output_artifact")
    if not isinstance(locator, Mapping):
        raise PromotionError("retained processor checkpoint has no output artifact locator")
    run_id, attempt_raw, name = processor_attempt_from_locator(checkpoint)
    bound_run = validate_trusted_processor_run(
        run,
        repository=repository,
        run_id=run_id,
        attempt=attempt_raw,
        default_branch=default_branch,
    )
    if (
        not isinstance(artifact_inventory, Mapping)
        or artifact_inventory.get("total_count") != 1
        or not isinstance(artifact_inventory.get("artifacts"), list)
        or len(artifact_inventory["artifacts"]) != 1
        or not isinstance(artifact_inventory["artifacts"][0], Mapping)
    ):
        raise PromotionError("retained processor artifact inventory is not exact")
    artifact = artifact_inventory["artifacts"][0]
    artifact_run = artifact.get("workflow_run")
    artifact_id = str(locator.get("artifact_id") or "")
    archive_digest = artifact.get("digest")
    archive_size = artifact.get("size_in_bytes")
    if (
        not artifact_id.isdigit()
        or str(artifact.get("id") or "") != artifact_id
        or artifact.get("name") != name
        or artifact.get("expired") is not False
        or artifact.get("expires_at") != locator.get("expires_at")
        or str(locator.get("repository") or "").casefold() != repository.casefold()
        or not isinstance(artifact_run, Mapping)
        or str(artifact_run.get("id") or "") != run_id
        or artifact_run.get("head_sha") != bound_run.get("head_sha")
        or artifact_run.get("head_branch") != default_branch
        or isinstance(archive_size, bool)
        or not isinstance(archive_size, int)
        or archive_size < 1
        or archive_size > MAX_PROCESSOR_ARCHIVE_BYTES
        or not isinstance(archive_digest, str)
        or not re.fullmatch(r"sha256:[a-f0-9]{64}", archive_digest)
        or not isinstance(archive_bytes, bytes)
        or len(archive_bytes) != archive_size
        or hashlib.sha256(archive_bytes).hexdigest() != archive_digest.removeprefix("sha256:")
    ):
        raise PromotionError("retained processor archive differs from its native run and artifact identity")
    source_reader.check_deadline()
    return (
        str(bound_run["head_sha"]), run_id, int(attempt_raw), artifact_id,
        archive_digest.removeprefix("sha256:"),
    )


def _validate_retained_processor_output_inventory(
    checkpoint: Mapping[str, Any], *, expected_outputs: Sequence[str],
) -> None:
    digests = checkpoint.get("output_digests")
    locator = checkpoint.get("output_artifact")
    if not isinstance(digests, list) or not isinstance(locator, Mapping):
        raise PromotionError("retained processor checkpoint is missing its output inventory")
    names: list[str] = []
    for row in digests:
        if (
            not isinstance(row, Mapping)
            or not isinstance(row.get("path"), str)
            or isinstance(row.get("bytes"), bool)
            or not isinstance(row.get("bytes"), int)
            or row["bytes"] < 1
            or not isinstance(row.get("sha256"), str)
            or not re.fullmatch(r"[a-f0-9]{64}", row["sha256"])
        ):
            raise PromotionError("retained processor output inventory is malformed")
        names.append(row["path"])
    if names != list(expected_outputs) or len(names) != len(set(names)):
        raise PromotionError("retained processor output inventory differs from its authenticated protocol")
    if hashlib.sha256(canonical_json(digests)).hexdigest() != locator.get("bundle_manifest_sha256"):
        raise PromotionError("retained processor output inventory digest differs from its checkpoint")


def _authenticate_historical_retained_processor_source(
    checkpoint: Mapping[str, Any], *, repository: str, producer_head_sha: str,
    cache_source_helper: Any, source_reader: Any,
) -> dict[str, bytes]:
    """Authenticate the closed pre-cache-profile source format used by retained 011."""
    generation_inputs = checkpoint.get("generation_inputs")
    exact_fields = {
        "source_id", "source_scope", "candidate_sha256", "baseline_sha256",
        "observation_failure_sha256", "policy_sha256", "adapter_revision",
        "extractor_revision", "generator_revision",
    }
    if (
        not isinstance(generation_inputs, Mapping)
        or set(generation_inputs) != exact_fields
        or generation_inputs.get("source_id") != "data_go_kr"
        or generation_inputs.get("source_scope") != "aggregate_supported_catalog"
        or generation_inputs.get("observation_failure_sha256") is not None
    ):
        raise PromotionError("retained historical processor generation fields are unsupported")
    source_bytes: dict[str, bytes] = {}
    for path, (expected_size, expected_sha256) in HISTORICAL_RETAINED_PROCESSOR_SOURCE_IDENTITIES.items():
        try:
            raw = source_reader.source_file(
                repository, producer_head_sha, path,
                maximum_bytes=cache_source_helper.MAX_SOURCE_FILE_BYTES,
            )
        except Exception as exc:
            raise PromotionError(f"retained historical processor source is unavailable: {path}") from exc
        if len(raw) != expected_size or hashlib.sha256(raw).hexdigest() != expected_sha256:
            raise PromotionError(f"retained historical processor source identity differs: {path}")
        source_bytes[path] = raw
    for field, path in PROCESSOR_INPUT_PROVENANCE.items():
        if generation_inputs.get(field) != hashlib.sha256(source_bytes[path]).hexdigest():
            raise PromotionError(f"retained historical checkpoint provenance differs: {path}")
    source_reader.check_deadline()
    return source_bytes


def classify_retained_processor_bundle_for_report(
    checkpoint: Mapping[str, Any],
    run: Mapping[str, Any],
    artifact_inventory: Mapping[str, Any],
    archive_bytes: bytes,
    *,
    repository: str,
    default_branch: str,
    cache_source_helper: Any,
    source_reader: Any,
    deadline_monotonic: float,
) -> RetainedProcessorReportContract:
    """Authenticate a retained report protocol before choosing ZIP members.

    The returned contract only selects an exact report ZIP shape. It carries no
    candidate, cache, or live-C capability, including for modern terminal data.
    """
    generation_inputs, _locator = _retained_processor_checkpoint_identity(checkpoint)
    producer_head_sha, run_id, attempt, artifact_id, archive_sha256 = (
        _retained_processor_native_identity(
            checkpoint, run, artifact_inventory, archive_bytes,
            repository=repository, default_branch=default_branch,
            cache_source_helper=cache_source_helper, source_reader=source_reader,
            deadline_monotonic=deadline_monotonic,
        )
    )
    profile = generation_inputs.get("cache_source_profile")
    cache_source_contract: Any | None = None
    if profile == cache_source_helper.CACHE_SOURCE_PROFILE:
        try:
            cache_source_contract = cache_source_helper.authenticate_owner_contract_from_bound_evidence(
                checkpoint,
                repository=repository,
                default_branch=default_branch,
                artifact_inventory=artifact_inventory,
                run=run,
                archive=archive_bytes,
                source_reader=source_reader,
                deadline_monotonic=deadline_monotonic,
            )
        except Exception as exc:
            raise PromotionError("retained modern processor protocol is unauthenticated") from exc
        if (
            type(cache_source_contract) is not cache_source_helper.AuthenticatedCacheContract
            or cache_source_contract.profile != cache_source_helper.CACHE_SOURCE_PROFILE
            or cache_source_contract.producer_head_sha != producer_head_sha
        ):
            raise PromotionError("retained modern processor capability is invalid")
        protocol = MODERN_RETAINED_PROCESSOR_PROTOCOL
        expected_outputs = REQUIRED_PROCESSOR_FILES
    elif profile is None:
        _authenticate_historical_retained_processor_source(
            checkpoint, repository=repository, producer_head_sha=producer_head_sha,
            cache_source_helper=cache_source_helper, source_reader=source_reader,
        )
        protocol = HISTORICAL_RETAINED_PROCESSOR_PROTOCOL
        expected_outputs = HISTORICAL_RETAINED_PROCESSOR_FILES
    else:
        raise PromotionError("retained processor source protocol is unsupported")
    processor_status = checkpoint.get("status")
    if protocol == MODERN_RETAINED_PROCESSOR_PROTOCOL:
        # Modern terminal reports have two producer-reachable closed shapes:
        # retry is emitted with the full ten-member bundle, while a quarantined
        # input terminal is emitted as the result-only marker.  Neither shape
        # carries a candidate or grants current/live-C eligibility.
        if processor_status in {"ready", "no-change", "retry"}:
            expected_outputs = REQUIRED_PROCESSOR_FILES
        elif processor_status == "quarantined":
            expected_outputs = RETAINED_PROCESSOR_RESULT_ONLY_FILES
        else:
            raise PromotionError("retained modern report status has no producer-owned bundle profile")
    elif processor_status not in {"ready", "no-change"}:
        # Preserve the historical eight-member report contract exactly.
        raise PromotionError("retained historical report protocol requires a reviewable processor output")
    _validate_retained_processor_output_inventory(
        checkpoint, expected_outputs=expected_outputs,
    )
    return RetainedProcessorReportContract(
        _RETAINED_REPORT_CAPABILITY,
        protocol=protocol,
        expected_members=(*expected_outputs, RETAINED_PROCESSOR_CHECKPOINT_MEMBER),
        checkpoint=checkpoint,
        producer_head_sha=producer_head_sha,
        run_id=run_id,
        run_attempt=attempt,
        artifact_id=artifact_id,
        archive_sha256=archive_sha256,
        archive_bytes=len(archive_bytes),
        source_reader=source_reader,
        cache_source_helper=cache_source_helper,
        repository=repository,
        default_branch=default_branch,
        cache_source_contract=cache_source_contract,
    )


def _validate_processor_bundle_content(
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
    repository: str | None = None,
    default_branch: str | None = None,
    cache_source_native_reader: Any | None = None,
    cache_source_helper: Any | None = None,
    cache_source_contract: Any | None = None,
    cache_source_archive_path: pathlib.Path | None = None,
    _retained_report_contract: RetainedProcessorReportContract | None = None,
    producer_composer_identity: Mapping[str, Any] | None = None,
    derivation_module: Any | None = None,
    replay_control: OriginalCReplayControlBinding | None = None,
    deadline_monotonic: float | None = None,
) -> dict[str, Any]:
    digests = checkpoint.get("output_digests")
    locator = checkpoint.get("output_artifact")
    if not isinstance(digests, list) or not isinstance(locator, dict):
        raise PromotionError("processor checkpoint is missing its output bundle inventory")
    names = [row.get("path") for row in digests if isinstance(row, dict)]
    processor_status = checkpoint.get("status")
    if processor_status in {"ready", "no-change"}:
        if names != list(REQUIRED_PROCESSOR_FILES):
            raise PromotionError("reviewable processor bundle inventory does not contain the exact ordered ten-file contract")
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
    report_only_quarantine = False
    if _retained_report_contract is not None:
        report_only_quarantine = _require_report_only_quarantine_capability(
            _retained_report_contract,
            checkpoint,
            root=root,
            producer_head_sha=producer_head_sha,
        )
    contract_diagnostics: list[dict[str, Any]] = []
    if root is not None and not report_only_quarantine:
        # Keep schema and unresolved-link checks on every terminal processor
        # status. Only the Seoul unchanged-decision check needs the resolved
        # composition-baseline identity below.
        contract_diagnostics = validate_processor_link_metadata(
            checkpoint, bundle_dir, root=root, validate_seoul=False,
            replay_control=replay_control, deadline_monotonic=deadline_monotonic,
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
    if report_only_quarantine:
        assert root is not None
        contract_diagnostics = _validate_report_only_quarantine_detail_metadata(
            checkpoint, bundle_dir, result, root=root,
        )
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
        if derivation_module is None:
            derivation_helper = load_module(
                (root / "scripts/upstream_catalogue_derivation.py") if root is not None
                else pathlib.Path(__file__).with_name("upstream_catalogue_derivation.py"),
                "promotion_same_observation_derivation",
            )
        else:
            module_name = getattr(derivation_module, "__name__", None)
            module_file = getattr(derivation_module, "__file__", None)
            expected_file = (
                root / "scripts/upstream_catalogue_derivation.py"
                if root is not None else pathlib.Path(__file__).with_name("upstream_catalogue_derivation.py")
            )
            try:
                expected_file = expected_file.resolve(strict=True)
                observed_file = pathlib.Path(module_file).resolve(strict=True) if isinstance(module_file, str) else None
            except OSError as exc:
                raise PromotionError("processor derivation helper singleton source is unavailable") from exc
            if (
                not isinstance(module_name, str) or not module_name
                or sys.modules.get(module_name) is not derivation_module
                or observed_file != expected_file
                or expected_file.is_symlink() or not expected_file.is_file()
            ):
                raise PromotionError("processor bundle requires the exact current derivation helper singleton")
            derivation_helper = derivation_module
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
    if contract_diagnostics and producer_composer_identity is not None:
        producer_composer = producer_composer_identity
        if (
            set(producer_composer) != {"bytes", "sha256"}
            or isinstance(producer_composer.get("bytes"), bool)
            or not isinstance(producer_composer.get("bytes"), int)
            or producer_composer["bytes"] < 1
            or not isinstance(producer_composer.get("sha256"), str)
            or not re.fullmatch(r"[a-f0-9]{64}", producer_composer["sha256"])
        ):
            raise PromotionError("processor bundle producer composer identity is malformed")
        diagnostic_encoding = authenticate_contract_diagnostic_encoding(
            composition, producer_composer,
        )
    elif contract_diagnostics and root is not None and producer_head_sha is not None:
        producer_composer = processor_composer_source_identity(root, producer_head_sha)
        diagnostic_encoding = authenticate_contract_diagnostic_encoding(composition, producer_composer)
    if root is not None or contract_diagnostics:
        validate_processor_contract_diagnostic_outputs(
            bundle_dir, contract_diagnostics, encoding=diagnostic_encoding,
        )
    if cache_source_helper is None or cache_source_contract is None:
        if root is None or not isinstance(repository, str) or not isinstance(default_branch, str):
            raise PromotionError("processor cache source producer contract is required")
        cache_source_helper, cache_source_contract = authenticate_processor_cache_source_contract(
            root, checkpoint,
            repository=repository,
            default_branch=default_branch,
            native_reader=cache_source_native_reader,
            archive_path=cache_source_archive_path,
        )
    if type(cache_source_contract) is not cache_source_helper.AuthenticatedCacheContract:
        raise PromotionError("processor cache source producer contract has an invalid capability type")
    if (
        cache_source_contract.profile != cache_source_helper.CACHE_SOURCE_PROFILE
        or names != list(REQUIRED_PROCESSOR_FILES)
    ):
        raise PromotionError("processor bundle inventory differs from its authenticated producer contract")
    try:
        context, original_candidate, provenance = cache_source_helper.validate_bound_context_bundle(
            context_path=bundle_dir / "upstream-catalogue-cache-source-context.json",
            owner_checkpoint=checkpoint,
            owner_contract=cache_source_contract,
        )
    except Exception as exc:
        raise PromotionError("processor cache source context is invalid") from exc
    if (
        provenance != "modern"
        or not isinstance(generation_inputs, Mapping)
        or context.get("candidate", {}).get("sha256") != generation_inputs.get("candidate_sha256")
        or file_sha256(original_candidate) != generation_inputs.get("candidate_sha256")
    ):
        raise PromotionError("processor cache source context does not bind its exact original A candidate")
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
        "_cache_source_helper": cache_source_helper,
        "_cache_source_contract": cache_source_contract,
    }
    if root is not None and not defer_seoul_declaration:
        validate_processor_seoul_bundle(
            checkpoint, bundle_dir, result, root=root,
            canonical_context=canonical_context,
            allow_terminal_noop=allow_terminal_noop,
            replay_control=replay_control, deadline_monotonic=deadline_monotonic,
        )
    return result


def _require_report_only_quarantine_capability(
    contract: RetainedProcessorReportContract,
    checkpoint: Mapping[str, Any],
    *,
    root: pathlib.Path | None,
    producer_head_sha: str | None,
) -> bool:
    """Allow the result-only exception only for one authenticated modern report."""
    if (
        type(contract) is not RetainedProcessorReportContract
        or contract.protocol != MODERN_RETAINED_PROCESSOR_PROTOCOL
        or root is None
        or producer_head_sha != contract._producer_head_sha
        or type(contract._cache_source_contract)
        is not contract._cache_source_helper.AuthenticatedCacheContract
    ):
        raise PromotionError("report-only quarantine exception lacks its authenticated modern source capability")
    generation_inputs = checkpoint.get("generation_inputs")
    outcome = checkpoint.get("outcome")
    if (
        checkpoint.get("status") != "quarantined"
        or not isinstance(generation_inputs, Mapping)
        or generation_inputs.get("cache_source_profile") != contract._cache_source_helper.CACHE_SOURCE_PROFILE
        or not isinstance(outcome, Mapping)
        or outcome.get("reason") != "composer_scope_omits_worker_outcomes"
        or contract.expected_members != (
            *RETAINED_PROCESSOR_RESULT_ONLY_FILES,
            RETAINED_PROCESSOR_CHECKPOINT_MEMBER,
        )
    ):
        raise PromotionError("report-only quarantine exception does not match its exact producer profile")
    _validate_retained_processor_output_inventory(
        checkpoint, expected_outputs=RETAINED_PROCESSOR_RESULT_ONLY_FILES,
    )
    return True


def _validate_report_only_quarantine_detail_metadata(
    checkpoint: Mapping[str, Any],
    bundle_dir: pathlib.Path,
    result: Mapping[str, Any],
    *,
    root: pathlib.Path,
) -> list[dict[str, Any]]:
    """Validate retained resolver metadata without inventing enrichment evidence."""
    outcome = checkpoint.get("outcome")
    if (
        checkpoint.get("status") != "quarantined"
        or not isinstance(outcome, Mapping)
        or outcome.get("reason") != "composer_scope_omits_worker_outcomes"
        or result.get("status") != "quarantined"
        or result.get("reason") != outcome.get("reason")
        or result.get("candidate_available") is not False
        or (bundle_dir / "upstream-catalogue-enrichment-evidence.json").exists()
    ):
        raise PromotionError("report-only quarantine metadata does not match its result-only marker")
    detail_records = checkpoint.get("detail_records")
    if not isinstance(detail_records, list):
        raise PromotionError("report-only quarantine detail records are invalid")
    try:
        composer = load_module(
            root / "scripts/compose-upstream-catalogue-candidate.py",
            "processor_catalogue_composer_for_report_only_quarantine",
        )
        helpers = getattr(composer, "LINK_DETAIL_HELPERS", None)
        validate_metadata = getattr(helpers, "validate_link_metadata", None)
        registered_hosts = helpers.registered_hosts(
            load_object(root / "data/provider-index.json"),
        )
    except (OSError, PromotionError, AttributeError, TypeError) as exc:
        raise PromotionError("trusted resolver metadata validators are unavailable") from exc
    if not callable(validate_metadata):
        raise PromotionError("trusted resolver metadata validator is unavailable")

    seen: set[str] = set()
    diagnostics: list[dict[str, Any]] = []
    for row in detail_records:
        if not isinstance(row, Mapping):
            raise PromotionError("report-only quarantine contains a malformed detail row")
        identity = str(row.get("id") or "")
        if identity:
            if identity in seen:
                raise PromotionError("report-only quarantine contains duplicate detail identities")
            seen.add(identity)
        diagnostic = row.get("failure_diagnostic")
        has_metadata = "link_metadata" in row
        has_contract_failure = (
            isinstance(diagnostic, Mapping)
            and diagnostic.get("code") == "resolved_link_operation_contract_unproven"
        )
        if has_contract_failure and not has_metadata:
            raise PromotionError("report-only resolver diagnostic is missing its link metadata provenance")
        if not has_metadata:
            continue
        if (
            not identity
            or row.get("status") != "quarantined"
            or not has_contract_failure
            or diagnostic.get("phase") != "resolver"
            or not isinstance(row.get("source_sha256"), str)
            or not re.fullmatch(r"[a-f0-9]{64}", row["source_sha256"])
            or row.get("guide_sha256") is not None
            and (
                not isinstance(row.get("guide_sha256"), str)
                or not re.fullmatch(r"[a-f0-9]{64}", row["guide_sha256"])
            )
        ):
            raise PromotionError("report-only resolver metadata is not bound to a quarantined detail row")
        try:
            validate_metadata(row["link_metadata"], identity, registered_hosts)
            projection = composer.worker_contract_projection(dict(row))
        except (ValueError, TypeError, AttributeError) as exc:
            raise PromotionError("report-only resolver metadata failed its strict row validation") from exc
        if not isinstance(projection, Mapping) or not projection:
            raise PromotionError("report-only resolver row has no trusted diagnostic projection")
        projected = {
            "api_key": {"provider": "data.go.kr", "id": identity},
            "worker_status": row.get("status"),
            "source_sha256": row.get("source_sha256"),
            "guide_sha256": row.get("guide_sha256"),
            "worker_outcome_sha256": projection.get("worker_outcome_sha256"),
            "detail_status": (
                "verified" if "contract_failure" in projection else "legacy_detail_unknown"
            ),
            "next_action": projection.get("next_action"),
        }
        if "contract_failure" in projection:
            projected["contract_failure"] = copy.deepcopy(projection["contract_failure"])
        elif projection.get("contract_failure_status") != "legacy_detail_unknown":
            raise PromotionError("report-only resolver legacy projection is invalid")
        diagnostics.append(projected)
    return sorted(
        diagnostics,
        key=lambda item: (str(item["api_key"]["provider"]), str(item["api_key"]["id"])),
    )


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
    repository: str | None = None,
    default_branch: str | None = None,
    cache_source_native_reader: Any | None = None,
    cache_source_helper: Any | None = None,
    cache_source_contract: Any | None = None,
    cache_source_archive_path: pathlib.Path | None = None,
    producer_composer_identity: Mapping[str, Any] | None = None,
    derivation_module: Any | None = None,
    replay_control: OriginalCReplayControlBinding | None = None,
    deadline_monotonic: float | None = None,
) -> dict[str, Any]:
    """Validate a live processor bundle without retained-report exceptions."""
    return _validate_processor_bundle_content(
        checkpoint,
        bundle_dir,
        composition_schema,
        composition_helper,
        root=root,
        producer_head_sha=producer_head_sha,
        canonical_context=canonical_context,
        allow_terminal_noop=allow_terminal_noop,
        defer_seoul_declaration=defer_seoul_declaration,
        repository=repository,
        default_branch=default_branch,
        cache_source_native_reader=cache_source_native_reader,
        cache_source_helper=cache_source_helper,
        cache_source_contract=cache_source_contract,
        cache_source_archive_path=cache_source_archive_path,
        producer_composer_identity=producer_composer_identity,
        derivation_module=derivation_module,
        replay_control=replay_control,
        deadline_monotonic=deadline_monotonic,
    )


def _validate_retained_report_contract_binding(
    contract: RetainedProcessorReportContract,
    checkpoint: Mapping[str, Any],
    *,
    root: pathlib.Path,
    producer_head_sha: str,
    source_reader: Any,
) -> None:
    if type(contract) is not RetainedProcessorReportContract:
        raise PromotionError("retained processor report contract has an invalid capability type")
    _retained_processor_checkpoint_identity(checkpoint)
    if (
        source_reader is not contract._source_reader
        or contract._producer_head_sha != producer_head_sha
        or contract._generation_id != checkpoint.get("generation_id")
        or contract._checkpoint_sha256 != checkpoint.get("checkpoint_sha256")
        or not root.is_dir()
        or pathlib.Path(source_reader.cwd).resolve() != root.resolve()
    ):
        raise PromotionError("retained processor report contract differs from its exact checkpoint or source")
    source_reader.check_deadline()


def _validate_retained_uploaded_checkpoint(
    checkpoint: Mapping[str, Any], bundle_dir: pathlib.Path,
) -> None:
    locator = checkpoint.get("output_artifact")
    if not isinstance(locator, Mapping):
        raise PromotionError("retained processor checkpoint has no output artifact locator")
    checkpoint_copy = load_object(bundle_dir / RETAINED_PROCESSOR_CHECKPOINT_MEMBER)
    if checkpoint_copy.get("generation_id") != checkpoint.get("generation_id"):
        raise PromotionError("uploaded retained processor checkpoint belongs to another generation")
    unsigned_copy = copy.deepcopy(checkpoint_copy)
    claimed_copy_sha = unsigned_copy.pop("checkpoint_sha256", None)
    if claimed_copy_sha != hashlib.sha256(canonical_json(unsigned_copy)).hexdigest():
        raise PromotionError("uploaded retained processor checkpoint receipt digest is invalid")
    copy_locator = checkpoint_copy.get("output_artifact")
    if not isinstance(copy_locator, Mapping) or copy_locator.get("artifact_id") is not None:
        raise PromotionError("uploaded retained processor checkpoint has an invalid artifact locator")
    copied_expiry = parse_utc_timestamp(copy_locator.get("expires_at"), "uploaded processor artifact expiry")
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
    generation_started_at = parse_utc_timestamp(
        checkpoint_copy.get("observed_at") or checkpoint.get("observed_at") or observed_at.isoformat(),
        "processor generation start",
    )
    if copied_heartbeat < generation_started_at or copied_heartbeat > final_heartbeat:
        raise PromotionError("uploaded retained processor checkpoint heartbeat is outside its timeline")
    if copied_expiry <= observed_at or copied_expiry > final_expiry:
        raise PromotionError("uploaded retained processor checkpoint expiry is outside its timeline")
    uploaded_normalized = copy.deepcopy(checkpoint_copy)
    durable_normalized = copy.deepcopy(dict(checkpoint))
    uploaded_normalized.pop("checkpoint_sha256", None)
    durable_normalized.pop("checkpoint_sha256", None)
    for normalized in (uploaded_normalized, durable_normalized):
        normalized["last_heartbeat_at"] = None
        normalized_locator = normalized.get("output_artifact")
        if isinstance(normalized_locator, dict):
            normalized_locator["artifact_id"] = None
            normalized_locator["expires_at"] = None
    if uploaded_normalized != durable_normalized:
        raise PromotionError("uploaded retained processor checkpoint differs from durable state")


def _retained_processor_source_facts(
    contract: RetainedProcessorReportContract,
    checkpoint: Mapping[str, Any],
    composition: Mapping[str, Any] | None,
    *,
    root: pathlib.Path,
) -> list[str]:
    """Verify producer-head provenance and report current source drift."""
    helper = contract._cache_source_helper
    reader = contract._source_reader
    repository = contract._repository
    producer_head_sha = contract._producer_head_sha
    generation_inputs = checkpoint.get("generation_inputs")
    if not isinstance(generation_inputs, Mapping):
        raise PromotionError("retained processor checkpoint lacks generation inputs")
    source_bytes: dict[str, bytes] = {}

    def producer_file(path: str) -> bytes:
        cached = source_bytes.get(path)
        if cached is not None:
            return cached
        try:
            raw = reader.source_file(
                repository, producer_head_sha, path,
                maximum_bytes=helper.MAX_SOURCE_FILE_BYTES,
            )
        except Exception as exc:
            raise PromotionError(f"retained processor producer source is unavailable: {path}") from exc
        source_bytes[path] = raw
        return raw

    for field, path in PROCESSOR_INPUT_PROVENANCE.items():
        if field == "generator_revision":
            continue
        if generation_inputs.get(field) != hashlib.sha256(producer_file(path)).hexdigest():
            raise PromotionError(f"retained processor checkpoint provenance differs: {path}")
    if contract.protocol == MODERN_RETAINED_PROCESSOR_PROTOCOL:
        try:
            expected_generator_revision = helper.modern_generator_revision(
                repository=repository, head_sha=producer_head_sha, reader=reader,
            )
        except Exception as exc:
            raise PromotionError("retained modern processor generator source is unavailable") from exc
        if generation_inputs.get("generator_revision") != expected_generator_revision:
            raise PromotionError("retained modern processor generator revision differs from producer source")
    elif generation_inputs.get("generator_revision") != hashlib.sha256(
        producer_file(PROCESSOR_INPUT_PROVENANCE["generator_revision"])
    ).hexdigest():
        raise PromotionError("retained historical processor generator revision differs from producer source")

    compatibility_paths = authenticated_processor_compatibility_paths(
        generation_inputs, root=root, producer_file=producer_file,
    )

    if composition is None:
        if checkpoint.get("status") not in {"retry", "quarantined"}:
            raise PromotionError("reviewable retained processor report lacks its composition receipt")
    else:
        input_digests = composition.get("input_digests")
        if not isinstance(input_digests, Mapping):
            raise PromotionError("retained processor composition receipt lacks source digests")
        for input_name, path in PROCESSOR_COMPOSITION_INPUTS.items():
            raw = producer_file(path)
            observed = input_digests.get(input_name)
            if (
                not isinstance(observed, Mapping)
                or observed.get("bytes") != len(raw)
                or observed.get("sha256") != hashlib.sha256(raw).hexdigest()
            ):
                raise PromotionError(
                    f"retained processor composition input differs from producer source: {path}"
                )

    changed: list[str] = []
    for path in compatibility_paths:
        try:
            historical = producer_file(path)
            current_path = root / path
            before = current_path.lstat()
            if (
                not stat.S_ISREG(before.st_mode) or stat.S_ISLNK(before.st_mode)
                or before.st_size < 1 or before.st_size > helper.MAX_SOURCE_FILE_BYTES
            ):
                raise OSError("current input is not a bounded regular file")
            current = current_path.read_bytes()
            after = current_path.lstat()
            if (
                len(current) != before.st_size
                or (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
                != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
            ):
                raise OSError("current input changed while reading")
            reader.check_deadline()
        except Exception:
            changed.append(path)
            continue
        if historical != current:
            changed.append(path)
    reader.check_deadline()
    return sorted(set(changed))


def _validate_report_only_retry_composition(
    checkpoint: Mapping[str, Any],
    bundle_dir: pathlib.Path,
    composition_schema: Mapping[str, Any],
    cache_source_helper: Any,
    *,
    cache_source_contract: Any | None,
    root: pathlib.Path,
    require_opaque_cache_contract: bool = True,
) -> Mapping[str, Any]:
    """Validate a retry report's full producer bundle without returning a candidate.

    The live-C bundle validator deliberately stops before candidate admission for
    retry.  The report reader still needs to bind the producer's receipt, its
    exact source inputs, all five composer outputs, and the original-A cache
    context.  This function returns the receipt only to its immediate caller;
    the public report facts never expose it or any candidate identity.
    """
    if checkpoint.get("status") != "retry":
        raise PromotionError("report-only retry validator received another terminal status")
    try:
        import jsonschema
    except ImportError as exc:
        raise PromotionError("report-only retry schema validator is unavailable") from exc

    generation_inputs = checkpoint.get("generation_inputs")
    locator = checkpoint.get("output_artifact")
    observation = checkpoint.get("last_observation")
    outcome = checkpoint.get("outcome")
    if (
        not isinstance(generation_inputs, Mapping)
        or not isinstance(locator, Mapping)
        or not isinstance(observation, Mapping)
        or not isinstance(outcome, Mapping)
    ):
        raise PromotionError("report-only retry checkpoint is missing its source or outcome identity")

    digests = checkpoint.get("output_digests")
    if not isinstance(digests, list) or [
        row.get("path") for row in digests if isinstance(row, Mapping)
    ] != list(REQUIRED_PROCESSOR_FILES):
        raise PromotionError("report-only retry does not carry the exact full-ten output inventory")
    output_rows = {str(row["path"]): row for row in digests if isinstance(row, Mapping)}
    composition_path = bundle_dir / "composition-receipt.json"
    composition = load_object(composition_path)
    try:
        jsonschema.Draft202012Validator(
            composition_schema, format_checker=jsonschema.FormatChecker(),
        ).validate(composition)
    except jsonschema.ValidationError as exc:
        raise PromotionError("report-only retry composer receipt does not match its schema") from exc

    try:
        processor = load_module(
            root / "scripts/process-upstream-catalogue-candidate.py",
            "report_only_retry_processor_validator",
        )
        composer_valid, composer_status, composer_receipt = processor.composer_outputs_valid(
            bundle_dir,
            str(generation_inputs.get("candidate_sha256") or ""),
            str(generation_inputs.get("baseline_sha256") or ""),
        )
    except (OSError, PromotionError, AttributeError, TypeError, ValueError) as exc:
        raise PromotionError("report-only retry producer validator is unavailable") from exc
    if (
        composer_valid is not True
        or composer_status != composition.get("status")
        or composer_receipt != composition
    ):
        raise PromotionError("report-only retry receipt fails the producer's actual output validator")

    status = composition.get("status")
    if status not in {"no_safe_change", "no_change"} or status != outcome.get("composer_status"):
        raise PromotionError("report-only retry composer status differs from its checkpoint outcome")
    reason = outcome.get("reason")
    pending_count = outcome.get("pending_count")
    retry_count = outcome.get("detail_retry_count")
    for label, value in (("pending", pending_count), ("detail retry", retry_count)):
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise PromotionError(f"report-only retry {label} count is invalid")
    if reason != "pending_detail_or_no_safe_change" or not (pending_count or retry_count):
        raise PromotionError("report-only retry lacks the producer's pending-work reason and counts")

    scope = composition.get("scope")
    if not isinstance(scope, Mapping) or scope.get("full_scope_fresh") is not False or scope.get("publication_allowed") is not False:
        raise PromotionError("report-only retry composer scope is not explicitly non-publishing")
    applied = scope.get("applied_api_keys")
    pending = scope.get("retained_pending_api_keys")
    quarantined = scope.get("quarantined_api_keys")
    if not isinstance(applied, list) or not isinstance(pending, list) or not isinstance(quarantined, list):
        raise PromotionError("report-only retry composer scope identity lists are invalid")

    def identity_set(values: list[Any], label: str) -> set[tuple[str, str]]:
        identities: list[tuple[str, str]] = []
        for value in values:
            if (
                not isinstance(value, Mapping)
                or not isinstance(value.get("provider"), str) or not value["provider"]
                or not isinstance(value.get("id"), str) or not value["id"]
            ):
                raise PromotionError(f"report-only retry {label} identity is malformed")
            identities.append((value["provider"].casefold(), value["id"]))
        if len(identities) != len(set(identities)):
            raise PromotionError(f"report-only retry {label} identities are duplicated")
        return set(identities)

    applied_ids = identity_set(applied, "applied")
    pending_ids = identity_set(pending, "pending")
    quarantined_ids = identity_set(quarantined, "quarantine")
    if applied_ids or pending_ids & quarantined_ids:
        raise PromotionError("report-only retry composer scope has conflicting terminal dispositions")
    if status == "no_safe_change" and not (pending_ids or quarantined_ids):
        raise PromotionError("report-only no-safe-change receipt has no pending or quarantined work")
    if pending_count != len(pending_ids | quarantined_ids):
        raise PromotionError("report-only retry pending count differs from composer scope identities")

    expected_composer_outputs = {
        "composed-candidate.registry.json", "ready-scope.registry.json", "semantic-diff.json",
        "regeneration-queue.json", "quarantine.json",
    }
    outputs = composition.get("outputs")
    if not isinstance(outputs, Mapping) or set(outputs) != expected_composer_outputs:
        raise PromotionError("report-only retry composer output inventory is not the exact five-file contract")
    for name, row in outputs.items():
        checkpoint_row = output_rows.get(name)
        output_path = bundle_dir / name
        if (
            not isinstance(row, Mapping) or not isinstance(checkpoint_row, Mapping)
            or row != {"bytes": checkpoint_row.get("bytes"), "sha256": checkpoint_row.get("sha256")}
            or output_path.is_symlink() or not output_path.is_file()
            or (output_path.stat().st_size, file_sha256(output_path))
            != (row.get("bytes"), row.get("sha256"))
        ):
            raise PromotionError(f"report-only retry composer output differs from its exact bundle digest: {name}")

    inputs = composition.get("input_digests")
    if not isinstance(inputs, Mapping):
        raise PromotionError("report-only retry composer receipt lacks its exact inputs")
    input_artifacts = checkpoint.get("input_artifacts")
    if not isinstance(input_artifacts, list):
        raise PromotionError("report-only retry checkpoint lacks its original producer artifact")
    producer_run_id = str(observation.get("producer_run_id") or "")
    producer_artifacts = [
        row for row in input_artifacts if isinstance(row, Mapping)
        and str(row.get("run_id") or "") == producer_run_id
    ]
    if len(producer_artifacts) != 1:
        raise PromotionError("report-only retry original producer artifact is ambiguous")
    producer_artifact = producer_artifacts[0]

    def require_digest(name: str, expected_sha256: Any, *, expected_bytes: int | None = None) -> Mapping[str, Any]:
        row = inputs.get(name)
        if (
            not isinstance(row, Mapping)
            or not isinstance(row.get("sha256"), str)
            or not re.fullmatch(r"[a-f0-9]{64}", row["sha256"])
            or isinstance(row.get("bytes"), bool)
            or not isinstance(row.get("bytes"), int)
            or row["bytes"] < 0
            or row.get("sha256") != expected_sha256
            or (expected_bytes is not None and row.get("bytes") != expected_bytes)
        ):
            raise PromotionError(f"report-only retry composer {name} input differs from its producer identity")
        return row

    candidate_member = "upstream-catalogue-original-candidate.registry.json"
    context_member = "upstream-catalogue-cache-source-context.json"
    candidate_row = output_rows.get(candidate_member)
    context_row = output_rows.get(context_member)
    candidate_path = bundle_dir / candidate_member
    context_path = bundle_dir / context_member
    if (
        not isinstance(candidate_row, Mapping) or not isinstance(context_row, Mapping)
        or candidate_path.is_symlink() or context_path.is_symlink()
        or not candidate_path.is_file() or not context_path.is_file()
        or (candidate_path.stat().st_size, file_sha256(candidate_path))
        != (candidate_row.get("bytes"), candidate_row.get("sha256"))
        or (context_path.stat().st_size, file_sha256(context_path))
        != (context_row.get("bytes"), context_row.get("sha256"))
    ):
        raise PromotionError("report-only retry original-A or cache context differs from its bundle inventory")
    if generation_inputs.get("cache_source_profile") != cache_source_helper.CACHE_SOURCE_PROFILE:
        raise PromotionError("report-only retry has no modern original-A cache-source profile")
    if require_opaque_cache_contract:
        if type(cache_source_contract) is not cache_source_helper.AuthenticatedCacheContract:
            raise PromotionError("report-only retry original-A cache contract is unauthenticated")
        try:
            context, original_candidate, provenance = cache_source_helper.validate_bound_context_bundle(
                context_path=context_path,
                owner_checkpoint=checkpoint,
                owner_contract=cache_source_contract,
            )
        except Exception as exc:
            raise PromotionError("report-only retry cache context or original-A binding is invalid") from exc
    else:
        try:
            context = cache_source_helper.validate_context(
                cache_source_helper.load_strict_json(
                    context_path, label="cache source context",
                    maximum_bytes=cache_source_helper.MAX_CONTEXT_BYTES,
                ),
            )
            admission = context["original_admission_ref"]
            cache_source_helper._checkpoint_input_binding(
                checkpoint, admission, context["candidate"]["sha256"],
            )
        except Exception as exc:
            raise PromotionError("report-only retry cache context or original-A binding is invalid") from exc
        original_candidate = candidate_path
        provenance = "modern"
        if (
            context.get("owner_generation_id") != checkpoint.get("generation_id")
            or context.get("source_id") != checkpoint.get("source_id")
            or context.get("source_scope") != checkpoint.get("source_scope")
            or context.get("candidate", {}).get("path") != candidate_member
            or context.get("candidate", {}).get("bytes") != candidate_path.stat().st_size
            or context.get("candidate", {}).get("sha256") != file_sha256(candidate_path)
        ):
            raise PromotionError("report-only retry cache context does not bind exact original-A bytes")
    if (
        provenance != "modern"
        or context.get("candidate", {}).get("sha256") != generation_inputs.get("candidate_sha256")
        or file_sha256(original_candidate) != generation_inputs.get("candidate_sha256")
    ):
        raise PromotionError("report-only retry cache source does not bind its exact original-A candidate")

    require_digest("baseline", generation_inputs.get("baseline_sha256"))
    require_digest(
        "candidate", generation_inputs.get("candidate_sha256"),
        expected_bytes=candidate_path.stat().st_size,
    )
    require_digest("full_diff", producer_artifact.get("diff_sha256"))
    require_digest("refresh_evidence", observation.get("refresh_evidence_sha256"))
    require_digest("provider_index", generation_inputs.get("adapter_revision"))
    require_digest("source_policy", generation_inputs.get("policy_sha256"))
    enrichment_row = output_rows.get("upstream-catalogue-enrichment-evidence.json")
    if not isinstance(enrichment_row, Mapping):
        raise PromotionError("report-only retry lacks its immutable enrichment evidence")
    require_digest(
        "enrichment_evidence", enrichment_row.get("sha256"),
        expected_bytes=enrichment_row.get("bytes"),
    )

    derivation = generation_inputs.get("same_observation_derivation")
    composition_baseline = inputs.get("composition_baseline")
    if derivation is None:
        if composition_baseline is not None or composition.get("same_observation_derivation") is not None:
            raise PromotionError("report-only retry contains an unbound composition baseline")
    else:
        try:
            derivation_helper = load_module(
                pathlib.Path(__file__).with_name("upstream_catalogue_derivation.py"),
                "report_only_retry_derivation",
            )
            validated_derivation = derivation_helper.validate_derivation_envelope(derivation)
        except Exception as exc:
            raise PromotionError("report-only retry derivation envelope is invalid") from exc
        expected_baseline = validated_derivation["composition_baseline"]
        if (
            composition.get("same_observation_derivation") != validated_derivation
            or not isinstance(composition_baseline, Mapping)
            or composition_baseline.get("sha256") != expected_baseline["registry_sha256"]
            or composition_baseline.get("bytes") != expected_baseline["registry_bytes"]
        ):
            raise PromotionError("report-only retry composition baseline differs from its authenticated derivation")

    producer = composition.get("producer")
    if (
        not isinstance(producer, Mapping)
        or producer.get("repository") != "StatPan/datapan-registry"
        or producer.get("run_id") != producer_run_id
        or producer.get("run_url") != f"https://github.com/StatPan/datapan-registry/actions/runs/{producer_run_id}"
    ):
        raise PromotionError("report-only retry composer receipt is bound to another producer run")

    semantic_path = bundle_dir / "semantic-diff.json"
    semantic_diff = load_object(semantic_path)
    for field, identities in (
        ("applied_api_keys", applied),
        ("retained_pending_api_keys", pending),
        ("quarantined_api_keys", quarantined),
    ):
        if semantic_diff.get(field) != identities:
            raise PromotionError(f"report-only retry composer scope differs from semantic diff: {field}")

    result = load_object(bundle_dir / "upstream-catalogue-processing-result.json")
    if (
        result.get("generation_id") != checkpoint.get("generation_id")
        or result.get("status") != "retry"
        or result.get("source_id") != checkpoint.get("source_id")
        or result.get("reason") != reason
        or result.get("candidate_available") is not False
        or result.get("processing_replay") is not False
        or str(result.get("producer_run_id") or "") != producer_run_id
        or str(result.get("processor_run_id") or "") != str(locator.get("name", "")).removeprefix(PROCESSING_PREFIX)
        or str(result.get("processor_artifact_run_id") or "") != str(locator.get("run_id") or "")
    ):
        raise PromotionError("report-only retry result does not bind its exact checkpoint")
    return composition


def _validate_historical_retained_processor_bundle(
    contract: RetainedProcessorReportContract,
    checkpoint: Mapping[str, Any],
    bundle_dir: pathlib.Path,
    composition_helper: Any,
    *,
    root: pathlib.Path,
) -> dict[str, Any]:
    """Read one authenticated pre-profile B packet without granting live-C authority."""
    if contract.protocol != HISTORICAL_RETAINED_PROCESSOR_PROTOCOL:
        raise PromotionError("historical retained processor reader received another protocol")
    _validate_retained_processor_output_inventory(
        checkpoint, expected_outputs=HISTORICAL_RETAINED_PROCESSOR_FILES,
    )
    digests = checkpoint.get("output_digests")
    assert isinstance(digests, list)
    for record in digests:
        assert isinstance(record, Mapping)
        path_name = str(record["path"])
        relative = pathlib.PurePosixPath(path_name)
        path = bundle_dir / path_name
        if (
            relative.is_absolute() or ".." in relative.parts or "\\" in path_name
            or not path.is_file() or path.is_symlink()
            or (path.stat().st_size, file_sha256(path))
            != (record["bytes"], record["sha256"])
        ):
            raise PromotionError(f"retained historical processor output differs: {path_name}")
    _validate_retained_uploaded_checkpoint(checkpoint, bundle_dir)

    try:
        import jsonschema
        historical_schema_raw = contract._source_reader.source_file(
            contract._repository, contract._producer_head_sha,
            "schemas/datapan.upstream-catalogue-checkpoint.v1.schema.json",
            maximum_bytes=contract._cache_source_helper.MAX_SOURCE_FILE_BYTES,
        )
        historical_checkpoint_schema = json.loads(historical_schema_raw)
        jsonschema.Draft202012Validator(
            historical_checkpoint_schema, format_checker=jsonschema.FormatChecker(),
        ).validate(dict(checkpoint))
    except Exception as exc:
        raise PromotionError("retained historical checkpoint fails its producer-head schema") from exc

    contract_diagnostics = validate_processor_link_metadata(
        checkpoint, bundle_dir, root=root, validate_seoul=False,
    )
    result = load_object(bundle_dir / "upstream-catalogue-processing-result.json")
    status = checkpoint.get("status")
    locator = checkpoint.get("output_artifact")
    observation = checkpoint.get("last_observation")
    outcome = checkpoint.get("outcome")
    if not isinstance(locator, Mapping) or not isinstance(outcome, Mapping):
        raise PromotionError("retained historical processor state lacks its outcome identity")
    locator_run_id = str(locator.get("run_id") or "")
    if (
        result.get("generation_id") != checkpoint.get("generation_id")
        or result.get("status") != status
        or result.get("source_id") != checkpoint.get("source_id")
        or result.get("producer_run_id")
        != (observation.get("producer_run_id") if isinstance(observation, Mapping) else None)
        or result.get("processor_run_id") != f"{locator_run_id}-{contract._run_attempt}"
        or str(result.get("processor_artifact_run_id") or "") != locator_run_id
        or result.get("processing_replay") is not False
        or result.get("candidate_available") is not (status == "ready")
        or result.get("reason") != outcome.get("reason")
    ):
        raise PromotionError("retained historical processor result differs from its checkpoint")

    candidate_path = bundle_dir / "composed-candidate.registry.json"
    receipt_path = bundle_dir / "composition-receipt.json"
    candidate_sha256 = file_sha256(candidate_path)
    composition = load_object(receipt_path)
    generation_inputs = checkpoint.get("generation_inputs")
    if not isinstance(generation_inputs, Mapping):
        raise PromotionError("retained historical checkpoint lacks generation inputs")
    input_digests = composition.get("input_digests")
    baseline_sha256 = generation_inputs.get("baseline_sha256")
    candidate_input_sha256 = generation_inputs.get("candidate_sha256")
    if not isinstance(input_digests, Mapping):
        raise PromotionError("retained historical composition lacks input digests")
    baseline_input = input_digests.get("baseline")
    candidate_input = input_digests.get("candidate")
    if (
        not isinstance(baseline_input, Mapping)
        or baseline_input.get("sha256") != baseline_sha256
        or isinstance(baseline_input.get("bytes"), bool)
        or not isinstance(baseline_input.get("bytes"), int)
        or baseline_input["bytes"] < 1
        or not isinstance(candidate_input, Mapping)
        or candidate_input.get("sha256") != candidate_input_sha256
    ):
        raise PromotionError("retained historical composition differs from checkpoint inputs")
    try:
        historical_composition_schema = json.loads(
            contract._source_reader.source_file(
                contract._repository, contract._producer_head_sha,
                PROCESSOR_COMPOSITION_INPUTS["receipt_schema"],
                maximum_bytes=contract._cache_source_helper.MAX_SOURCE_FILE_BYTES,
            )
        )
        composition_helper.validate_composition(
            composition, bundle_dir, historical_composition_schema, candidate_sha256,
            expected_status="ready_scoped" if status == "ready" else "no_change",
        )
    except Exception as exc:
        raise PromotionError("retained historical composition receipt is invalid") from exc
    changed_paths = _retained_processor_source_facts(
        contract, checkpoint, composition, root=root,
    )
    validate_processor_contract_diagnostic_outputs(
        bundle_dir, contract_diagnostics, encoding="historical",
    )
    if status == "no-change" and (
        candidate_sha256 != baseline_sha256
        or outcome.get("composer_status") != "no_change"
        or int(outcome.get("pending_count", -1)) != 0
        or int(outcome.get("detail_retry_count", -1)) != 0
    ):
        raise PromotionError("retained historical no-change proof is inconsistent")
    return {
        "status": status,
        "registry_path": "data/data-go-kr.registry.json",
        "registry_bytes": candidate_path.stat().st_size,
        "registry_sha256": candidate_sha256,
        "baseline_sha256": baseline_sha256,
        "composition_baseline_sha256": baseline_sha256,
        "composition_baseline_bytes": baseline_input["bytes"],
        "original_baseline_sha256": baseline_sha256,
        "producer_candidate_sha256": candidate_input_sha256,
        "composition_receipt": composition,
        "composition_receipt_path": str(receipt_path.resolve()),
        "composition_receipt_sha256": file_sha256(receipt_path),
        "composition_outputs_dir": str(bundle_dir.resolve()),
        "contract_diagnostics": contract_diagnostics,
        "current_input_contract_compatible": False,
        "current_input_contract_changed_paths": changed_paths,
        "current_input_contract_reason_code": HISTORICAL_RETAINED_PROCESSOR_CURRENT_REASON,
    }


def validate_retained_processor_bundle_for_report(
    contract: RetainedProcessorReportContract,
    checkpoint: Mapping[str, Any],
    bundle_dir: pathlib.Path,
    composition_schema: Mapping[str, Any],
    composition_helper: Any,
    *,
    root: pathlib.Path,
    producer_head_sha: str,
    source_reader: Any,
) -> RetainedProcessorReportBundle:
    """Validate retained B facts for a report without broadening live-C admission."""
    _validate_retained_report_contract_binding(
        contract, checkpoint, root=root, producer_head_sha=producer_head_sha,
        source_reader=source_reader,
    )
    if contract.protocol == MODERN_RETAINED_PROCESSOR_PROTOCOL:
        outcome = checkpoint.get("outcome")
        report_only_quarantine = (
            checkpoint.get("status") == "quarantined"
            and isinstance(outcome, Mapping)
            and outcome.get("reason") == "composer_scope_omits_worker_outcomes"
        )
        validation = (
            _validate_processor_bundle_content
            if report_only_quarantine else validate_processor_bundle
        )
        validation_kwargs: dict[str, Any] = {
            "root": root,
            "producer_head_sha": producer_head_sha,
            "defer_seoul_declaration": True,
            "cache_source_helper": contract._cache_source_helper,
            "cache_source_contract": contract.cache_source_contract,
        }
        if report_only_quarantine:
            validation_kwargs["_retained_report_contract"] = contract
        bundle = validation(
            checkpoint, bundle_dir, composition_schema, composition_helper,
            **validation_kwargs,
        )
        composition = bundle.get("composition_receipt")
        if composition is not None and not isinstance(composition, Mapping):
            raise PromotionError("retained modern processor report lacks a composition receipt")
        if checkpoint.get("status") == "retry":
            composition = _validate_report_only_retry_composition(
                checkpoint,
                bundle_dir,
                composition_schema,
                contract._cache_source_helper,
                cache_source_contract=contract.cache_source_contract,
                root=root,
            )
        if composition is None and checkpoint.get("status") not in {"retry", "quarantined"}:
            raise PromotionError("reviewable retained modern processor report lacks a composition receipt")
        changed_paths = _retained_processor_source_facts(
            contract, checkpoint, composition, root=root,
        )
        facts = dict(bundle)
        facts.pop("_cache_source_helper", None)
        facts.pop("_cache_source_contract", None)
        facts.update({
            "current_input_contract_compatible": not changed_paths,
            "current_input_contract_changed_paths": changed_paths,
            "current_input_contract_reason_code": (
                None if not changed_paths else "modern_processor_input_contract_changed"
            ),
        })
    elif contract.protocol == HISTORICAL_RETAINED_PROCESSOR_PROTOCOL:
        facts = _validate_historical_retained_processor_bundle(
            contract, checkpoint, bundle_dir, composition_helper, root=root,
        )
    else:
        raise PromotionError("retained processor report contract protocol is unsupported")
    return RetainedProcessorReportBundle(
        _RETAINED_REPORT_CAPABILITY, protocol=contract.protocol, facts=facts,
    )


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
            repository=repository,
            default_branch=default_branch,
            cache_source_archive_path=bundle_dir.with_suffix(".zip"),
        )
        verify_processor_input_compatibility(
            root, checkpoint, str(run["head_sha"]), current_head_sha,
            composition_receipt=bundle.get("composition_receipt"),
            cache_source_helper=bundle.get("_cache_source_helper"),
            cache_source_contract=bundle.get("_cache_source_contract"),
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
    "scripts/upstream_catalogue_cache_source.py",
    "scripts/compose-upstream-catalogue-candidate.py",
    "scripts/generate-batch-link-detail-registry-patches.py",
    "scripts/seoul_oa109_operation_declaration.py",
    "scripts/upstream_catalogue_derivation.py",
    "scripts/materialize-canonical-registry.py",
    "scripts/check-upstream-catalogue-health.py",
    "scripts/canonical_update_terminal_evidence.py",
    "scripts/canonical_stale_queue_recovery.py",
    "scripts/canonical_stale_queue_recovery_gate.py",
    "scripts/canonical_stale_queue_state.py",
    "scripts/canonical_publication_state.py",
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
    "schemas/datapan.canonical-stale-c-queue-recovery-receipt.v1.schema.json",
    "schemas/datapan.canonical-stale-c-queue-recovery.v1.schema.json",
    "schemas/datapan.canonical-stale-queue-recovery-policy.v1.schema.json",
    "policy/upstream-catalogue-health.json",
    "policy/canonical-stale-queue-recovery.v1.json",
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
        repository=repo,
        default_branch=os.environ.get("GITHUB_DEFAULT_BRANCH", "main"),
        cache_source_helper=getattr(args, "_cache_source_helper", None),
        cache_source_contract=getattr(args, "_cache_source_contract", None),
        cache_source_archive_path=getattr(args, "_cache_source_archive_path", None),
    )
    verify_processor_input_compatibility(
        root, checkpoint, args.workflow_run_head_sha, head_sha,
        composition_receipt=bundle.get("composition_receipt"),
        cache_source_helper=bundle.get("_cache_source_helper"),
        cache_source_contract=bundle.get("_cache_source_contract"),
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
    args._cache_source_helper = screened["bundle"].get("_cache_source_helper")
    args._cache_source_contract = screened["bundle"].get("_cache_source_contract")
    args._cache_source_archive_path = screened["bundle_dir"].with_suffix(".zip")
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
        repository=repository,
        default_branch=default_branch,
        cache_source_archive_path=bundle_dir.with_suffix(".zip"),
    )
    verify_processor_input_compatibility(
        root, checkpoint, str(run["head_sha"]), target_main,
        composition_receipt=bundle.get("composition_receipt"),
        cache_source_helper=bundle.get("_cache_source_helper"),
        cache_source_contract=bundle.get("_cache_source_contract"),
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
    args._cache_source_helper = bundle.get("_cache_source_helper")
    args._cache_source_contract = bundle.get("_cache_source_contract")
    args._cache_source_archive_path = bundle_dir.with_suffix(".zip")
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




# #754 source-target / Native replay and release-closure additions.
_RESOLVED_SOURCE_TARGET = object()

_RESOLVED_SOURCE_ELIGIBILITY = object()

_VALIDATED_SOURCE_TARGET_NOOP_EVALUATION = object()

_ORIGINAL_C_REPLAY_EVIDENCE = object()

_ORIGINAL_C_REPLAY_CONTROL_BINDING = object()

RELEASE_CLOSURE_COMPARISON_TOP_FIELDS = frozenset({
    "schema_version", "purpose", "release_closure_reference", "admitted_parent_ref",
    "original_generation", "source_eligibility_evidence", "source_refresh_evidence_sha256",
    "generator_consumer_closure_sha256", "provenance_dependency_digests", "comparison",
})

RELEASE_CLOSURE_COMPARISON_LEGACY_TOP_FIELDS = frozenset({
    "schema_version", "purpose", "release_closure_reference", "parent_reference",
    "original_generation", "source_eligibility_evidence", "source_refresh_evidence_sha256",
    "generator_consumer_closure_sha256", "provenance_dependency_digests", "comparison",
})

RELEASE_CLOSURE_COMPARISON_FIELDS = frozenset({
    "ruleset_sha256", "canonical_sha256", "canonical_representation",
    "canonical_git_blob_sha1", "canonical_git_blob_bytes", "canonical_git_blob_sha256",
    "admitted_parent_ref_sha256", "current_preparation_base",
    "admitted_parent_projection_sha256", "current_base_projection_sha256",
    "regenerated_projection_sha256", "parent_delta_paths", "current_base_delta_paths",
    "parent_delta_records", "current_base_delta_records", "current_base_delta_sha256",
    "captured_changed_paths", "projection_input_bounds", "source_eligibility_evidence_sha256",
    "original_generation_sha256", "original_generation_epoch", "original_epoch_origin",
    "original_observed_at",
})

RELEASE_CLOSURE_COMPARISON_LEGACY_FIELDS = frozenset({
    "ruleset_sha256", "canonical_sha256", "canonical_representation",
    "canonical_git_blob_sha1", "canonical_git_blob_bytes", "canonical_git_blob_sha256",
    "legacy_baseline_id", "current_preparation_base",
    "legacy_parent_projection_sha256", "current_base_projection_sha256",
    "generated_projection_sha256", "parent_delta_paths", "current_base_delta_paths",
    "parent_delta_records", "current_base_delta_records", "current_base_delta_sha256",
    "captured_changed_paths", "projection_input_bounds", "source_eligibility_evidence_sha256",
    "original_generation_sha256", "original_generation_epoch", "original_epoch_origin",
    "original_observed_at",
})

RELEASE_CLOSURE_LEGACY_BASELINE_FIELDS = frozenset({
    "schema_version", "role", "baseline_id", "identity", "evidence",
})

RELEASE_CLOSURE_LEGACY_BASELINE_IDENTITY_FIELDS = frozenset({
    "repository", "source_id", "scope", "generation_id", "candidate_head_sha",
    "candidate_base_sha", "merged_pr_number", "merged_sha", "ownership",
    "release_manifest", "canonical_registry",
})

RELEASE_CLOSURE_LEGACY_BASELINE_EVIDENCE_FIELDS = frozenset({
    "journal", "merged_pr", "merge_ack", "source_contract", "retained_inputs",
})

RELEASE_CLOSURE_LEGACY_REFERENCE_FIELDS = frozenset({
    "schema_version", "purpose", "refresh_key", "legacy_baseline_id",
    "generation_id", "canonical_sha256", "legacy_parent_projection_sha256",
    "current_base_delta_sha256", "generated_projection_sha256", "branch",
})

RELEASE_CLOSURE_LEGACY_PARENT_FIELDS = frozenset({"kind", "reference"})

MAX_RELEASE_CLOSURE_COMPARISON_BYTES = 64 * 1024 * 1024

MAX_NATIVE_RELEASE_CLOSURE_COMPARISON_BLOB_BYTES = 8 * 1024 * 1024

MAX_CURRENT_CONTROL_SOURCE_BYTES = 8 * 1024 * 1024

MAX_RELEASE_CLOSURE_MEMBER_PATHS = 1024

RELEASE_CLOSURE_GIT_COMMANDS_PER_BLOB = 4

RELEASE_CLOSURE_GIT_COMMAND_SETUP_RESERVE = 64

_CURRENT_CONTROL_STATE_MODULES: dict[
    str, tuple[types.ModuleType, tuple[str, str, str, str], bytes],
] = {}

_CONTROL_STATE_MISSING = object()

_CONTROL_SOURCE_MODULES: dict[
    str, tuple[types.ModuleType, tuple[str, ...], bytes],
] = {}

_CONTROL_SOURCE_RESOURCE_BASELINES: dict[tuple[str, str, str], bytes] = {}

_CONTROL_SOURCE_MODULE_PATHS = {
    "canonical_update_pr_release_closure_ack": "scripts/canonical_update_pr.py",
    "canonical_release_closure_frozen_v3": "scripts/canonical_release_closure.py",
    "canonical_release_closure_native_v2_ack": "scripts/canonical_release_closure_native_v2.py",
    "upstream_catalogue_source_eligibility_c2_ack": "scripts/upstream_catalogue_source_eligibility.py",
    "upstream_catalogue_derivation_c2_ack": "scripts/upstream_catalogue_derivation.py",
}

_CONTROL_SOURCE_RESOURCE_PATHS = frozenset({
    "scripts/canonical_release_closure_rules_v1.json",
    "scripts/canonical_release_generator_consumer_membership.v3.json",
})

_CONTROL_ACK_MODULES_SEAL = object()

_ACK_RUNTIME_MAX_DEPTH = 20

_ACK_RUNTIME_MAX_ITEMS = 8192

class ResolvedSourceTarget:
    """Runner-owned capability resolved before any mutable C-parent choice."""

    __slots__ = (
        "selection_key", "reason_code", "evaluation", "original_admission", "original_observation",
        "original_generation", "target_contract", "target_inventory", "generation_id",
        "state_refs", "_state_snapshot",
    )

    def __init__(
        self,
        token: object,
        *,
        selection_key: str,
        reason_code: str,
        evaluation: Mapping[str, Any],
        original_admission: Mapping[str, Any],
        original_observation: Mapping[str, Any],
        original_generation: Mapping[str, Any],
        target_contract: Mapping[str, Any],
        target_inventory: Sequence[Mapping[str, Any]],
        generation_id: str,
        state_refs: Sequence[Mapping[str, Any]],
        state_snapshot: Mapping[str, Any],
    ) -> None:
        if token is not _RESOLVED_SOURCE_TARGET:
            raise PromotionError("resolved source target constructor is private")
        if (
            not re.fullmatch(r"[a-f0-9]{64}", selection_key)
            or not re.fullmatch(r"[a-f0-9]{64}", generation_id)
            or not isinstance(reason_code, str)
            or not reason_code
            or original_generation.get("generation_id") != generation_id
        ):
            raise PromotionError("resolved source target identity is invalid")
        self.selection_key = selection_key
        self.reason_code = reason_code
        self.evaluation = copy.deepcopy(dict(evaluation))
        self.original_admission = copy.deepcopy(dict(original_admission))
        self.original_observation = copy.deepcopy(dict(original_observation))
        self.original_generation = copy.deepcopy(dict(original_generation))
        self.target_contract = copy.deepcopy(dict(target_contract))
        self.target_inventory = copy.deepcopy(list(target_inventory))
        self.generation_id = generation_id
        self.state_refs = copy.deepcopy(list(state_refs))
        self._state_snapshot = copy.deepcopy(dict(state_snapshot))

class ResolvedSourceEligibility:
    """Runner-owned capability binding eligibility to one screened B artifact."""

    __slots__ = (
        "selection", "original_generation", "target_contract", "source_epoch",
        "processor_candidate",
    )

    def __init__(self, token: object, verified: Any, processor_candidate: Mapping[str, Any]) -> None:
        if token is not _RESOLVED_SOURCE_ELIGIBILITY:
            raise PromotionError("resolved source eligibility constructor is private")
        selection = getattr(verified, "selection", None)
        if (
            not isinstance(selection, Mapping)
            or selection.get("action") != "revalidate_c"
            or not isinstance(processor_candidate, Mapping)
            or not isinstance(processor_candidate.get("checkpoint"), Mapping)
            or processor_candidate.get("generation_id")
            != selection.get("evidence", {}).get("original_generation", {}).get("generation_id")
            or processor_candidate["checkpoint"].get("generation_id")
            != processor_candidate.get("generation_id")
        ):
            raise PromotionError("resolved source eligibility processor binding is invalid")
        self.selection = copy.deepcopy(dict(selection))
        self.original_generation = copy.deepcopy(dict(verified.original_generation))
        self.target_contract = copy.deepcopy(dict(verified.target_contract))
        self.source_epoch = copy.deepcopy(dict(verified.source_epoch))
        self.processor_candidate = copy.deepcopy(dict(processor_candidate))

class OriginalCReplayEvidence:
    """Runner-owned bundle of same-resolver Native facts for original C replay.

    This is acquisition only. It does not grant source eligibility; that is
    minted later after exact checkpoint, B bundle, target-contract, parent,
    and full-selection recomputation succeed.
    """

    __slots__ = (
        "native_resolver", "native_module", "facts", "control", "decoded",
        "processor_state", "processor_artifact", "parent_facts", "expected_ref",
        "journal_snapshot", "requested_pr_number", "deadline_monotonic",
    )

    def __init__(
        self, token: object, *, native_resolver: Any, native_module: Any,
        facts: Any, control: Any, decoded: Any, processor_state: Any,
        processor_artifact: Any, parent_facts: Any, expected_ref: Mapping[str, Any],
        journal_snapshot: Mapping[str, Any], requested_pr_number: int,
        deadline_monotonic: float,
    ) -> None:
        if token is not _ORIGINAL_C_REPLAY_EVIDENCE:
            raise PromotionError("original C replay evidence constructor is private")
        self.native_resolver = native_resolver
        self.native_module = native_module
        self.facts = facts
        self.control = control
        self.decoded = decoded
        self.processor_state = processor_state
        self.processor_artifact = processor_artifact
        self.parent_facts = parent_facts
        self.expected_ref = copy.deepcopy(dict(expected_ref))
        self.journal_snapshot = copy.deepcopy(dict(journal_snapshot))
        self.requested_pr_number = requested_pr_number
        self.deadline_monotonic = float(deadline_monotonic)

class OriginalCReplayControlBinding:
    """Runner-owned view of Native's closed current-CONTROL replay handle.

    The Native capability remains the authority for every helper/data read.
    This wrapper only narrows the names the replay path may consume and makes
    each access revalidate the same resolver-issued facts and deadline.
    """

    __slots__ = ("replay", "source_facts", "native_control", "deadline_monotonic")

    _MODULE_ACCESSORS = {
        "runner": "runner_module",
        "eligibility": "eligibility_module",
        "handoff": "handoff_module",
        "derivation": "derivation_module",
        "preparer": "preparer_module",
        "processor": "processor_module",
        "health": "health_module",
        "canonical_update_pr": "canonical_update_pr_module",
        "composer": "composer_module",
        "link_detail": "link_detail_module",
        "seoul_declaration": "seoul_declaration_module",
        "terminal_evidence": "terminal_evidence_module",
    }
    _DATA_ACCESSORS = {
        "checkpoint_schema": "checkpoint_schema",
        "composition_schema": "composition_schema",
        "enrichment_schema": "enrichment_schema",
        "specs_schema": "specs_schema",
        "health_policy": "health_policy",
        "provider_index": "provider_index",
        "seoul_operation_declaration": "seoul_operation_declaration",
        "historical_subject_declaration": "historical_subject_declaration",
    }

    def __init__(
        self,
        token: object,
        *,
        replay: OriginalCReplayEvidence,
        source_facts: Any,
        native_control: Any,
    ) -> None:
        if token is not _ORIGINAL_C_REPLAY_CONTROL_BINDING:
            raise PromotionError("original C replay CONTROL binding constructor is private")
        self.replay = replay
        self.source_facts = source_facts
        self.native_control = native_control
        self.deadline_monotonic = replay.deadline_monotonic

    def require(self, *, deadline_monotonic: float) -> Any:
        replay = self.replay
        native_module = replay.native_module
        if (
            type(replay) is not OriginalCReplayEvidence
            or isinstance(deadline_monotonic, bool)
            or not isinstance(deadline_monotonic, (int, float))
            or not math.isfinite(deadline_monotonic)
            or float(deadline_monotonic) != self.deadline_monotonic
            or float(deadline_monotonic) != replay.deadline_monotonic
            or deadline_monotonic <= time.monotonic()
            or native_module is not sys.modules.get(getattr(native_module, "__name__", ""))
            or replay.native_resolver is None
        ):
            raise PromotionError("original C replay CONTROL binding deadline or context is invalid")
        require_facts = getattr(native_module, "require_verified_native_owned_c_revision_facts", None)
        require_source = getattr(native_module, "require_verified_original_source_contract_facts", None)
        require_control = getattr(native_module, "require_verified_original_replay_control", None)
        if not all(callable(item) for item in (require_facts, require_source, require_control)):
            raise PromotionError("original C replay CONTROL validators are unavailable")
        try:
            if require_facts(replay.facts) is not replay.facts:
                raise PromotionError("original C preliminary facts changed during replay")
            if require_source(self.source_facts) is not self.source_facts:
                raise PromotionError("original C source facts changed during replay")
            if require_control(
                self.native_control, deadline_monotonic=deadline_monotonic,
            ) is not self.native_control:
                raise PromotionError("original C replay CONTROL handle changed during replay")
        except PromotionError:
            raise
        except Exception as exc:
            raise PromotionError("original C replay CONTROL drift check failed") from exc
        if (
            getattr(self.native_control, "runner_module", None)
            is not sys.modules.get(__name__)
            or getattr(self.source_facts, "runner_module", None)
            is not getattr(self.native_control, "runner_module", None)
            or getattr(self.native_control, "eligibility_control", None) is not replay.control
            or getattr(self.source_facts, "eligibility_control", None) is not replay.control
            or getattr(self.native_control, "eligibility_module", None)
            is not getattr(replay.control, "eligibility_module", None)
        ):
            raise PromotionError("original C replay CONTROL changed runner or eligibility singleton")
        return self.native_control

    def module(self, name: str, *, deadline_monotonic: float) -> Any:
        accessor = self._MODULE_ACCESSORS.get(name)
        if accessor is None:
            raise PromotionError("original C replay requested an unregistered CONTROL module")
        control = self.require(deadline_monotonic=deadline_monotonic)
        value = getattr(control, accessor, None)
        if (
            not isinstance(value, type(sys))
            or not isinstance(getattr(value, "__name__", None), str)
            or sys.modules.get(value.__name__) is not value
        ):
            raise PromotionError(f"original C replay CONTROL module is not registered: {name}")
        return value

    def data(self, name: str, *, deadline_monotonic: float) -> Any:
        accessor = self._DATA_ACCESSORS.get(name)
        if accessor is None:
            raise PromotionError("original C replay requested unregistered CONTROL data")
        control = self.require(deadline_monotonic=deadline_monotonic)
        value = getattr(control, accessor, None)
        if not isinstance(value, Mapping):
            raise PromotionError(f"original C replay CONTROL data is unavailable: {name}")
        try:
            return copy.deepcopy(dict(value))
        except Exception as exc:
            raise PromotionError(f"original C replay CONTROL data is not copyable: {name}") from exc

def acquire_original_c_replay_evidence(
    *,
    native_resolver: Any,
    native_module: Any,
    expected_ref: Any,
    requested_pr_number: int,
    deadline_monotonic: float,
) -> OriginalCReplayEvidence:
    """Acquire the exact preliminary/state/B/parent facts through one resolver.

    The receiver must call this only after its native caller/ref checks. Every
    acquired capability is minted by the same Native resolver; this function
    does not accept caller-selected generations, run IDs, artifact IDs, or
    parent records and does not perform fallback GitHub/Git/Health reads.
    """
    if (
        isinstance(deadline_monotonic, bool)
        or not isinstance(deadline_monotonic, (int, float))
        or not math.isfinite(deadline_monotonic)
        or deadline_monotonic <= time.monotonic()
        or isinstance(requested_pr_number, bool)
        or not isinstance(requested_pr_number, int)
        or requested_pr_number < 1
        or not isinstance(native_module, type(sys))
        or not isinstance(getattr(native_module, "__name__", None), str)
        or sys.modules.get(native_module.__name__) is not native_module
        or not callable(getattr(native_resolver, "read_original_c_journal_snapshot", None))
        or not callable(getattr(native_resolver, "read_owned_c_revision_facts", None))
    ):
        raise PromotionError("original C replay Native context is unavailable or malformed")
    resolver_type = getattr(native_module, "NativeOwnedCRevisionResolver", None)
    if not isinstance(resolver_type, type) or type(native_resolver) is not resolver_type:
        raise PromotionError("original C replay requires the exact Native resolver type")
    try:
        journal_snapshot = native_resolver.read_original_c_journal_snapshot(
            expected_ref=expected_ref,
            deadline_monotonic=deadline_monotonic,
        )
        require_snapshot = getattr(
            native_module, "require_verified_original_c_journal_snapshot", None,
        )
        if not callable(require_snapshot) or require_snapshot(journal_snapshot) is not journal_snapshot:
            raise PromotionError("original C journal snapshot is not the exact Native-issued capability")
        facts = native_resolver.read_owned_c_revision_facts(
            expected_ref=expected_ref,
            journal_snapshot=journal_snapshot,
            requested_pr_number=requested_pr_number,
            deadline_monotonic=deadline_monotonic,
        )
        require_facts = getattr(native_module, "require_verified_native_owned_c_revision_facts", None)
        if not callable(require_facts) or require_facts(facts) is not facts:
            raise PromotionError("original C preliminary facts failed exact Native validation")
        control = native_resolver.original_source_eligibility_control(
            expected_facts=facts, deadline_monotonic=deadline_monotonic,
        )
        require_control = getattr(native_module, "require_verified_original_source_eligibility_control", None)
        if not callable(require_control) or require_control(control) is not control:
            raise PromotionError("original C current-CONTROL source validator is not Native sealed")
        decoded = native_resolver.decode_original_source_selection(
            expected_facts=facts, deadline_monotonic=deadline_monotonic,
        )
        require_decoded = getattr(native_module, "require_decoded_original_source_selection", None)
        if not callable(require_decoded) or require_decoded(decoded) is not decoded:
            raise PromotionError("original C source selection is not Native decoded")
        state = native_resolver.read_original_processor_state(
            expected_facts=facts, deadline_monotonic=deadline_monotonic,
        )
        require_state = getattr(native_module, "require_verified_original_processor_state", None)
        if not callable(require_state) or require_state(state) is not state:
            raise PromotionError("original B processor state is not Native sealed")
        artifact = native_resolver.read_original_processor_artifact(
            expected_facts=facts, state=state,
            deadline_monotonic=deadline_monotonic,
        )
        require_artifact = getattr(native_module, "require_verified_original_processor_artifact", None)
        if not callable(require_artifact) or require_artifact(artifact) is not artifact:
            raise PromotionError("original B artifact is not Native sealed")
        parent_facts = native_resolver.read_original_canonical_parent_facts(
            expected_facts=facts, processor_state=state,
            deadline_monotonic=deadline_monotonic,
        )
        require_parent = getattr(native_module, "require_verified_original_canonical_parent_facts", None)
        if not callable(require_parent) or require_parent(parent_facts) is not parent_facts:
            raise PromotionError("original C parent facts are not Native sealed")
    except PromotionError:
        raise
    except Exception as exc:
        raise PromotionError("original C replay Native acquisition failed closed") from exc
    return OriginalCReplayEvidence(
        _ORIGINAL_C_REPLAY_EVIDENCE,
        native_resolver=native_resolver, native_module=native_module,
        facts=facts, control=control, decoded=decoded,
        processor_state=state, processor_artifact=artifact,
        parent_facts=parent_facts, expected_ref=expected_ref,
        journal_snapshot=journal_snapshot, requested_pr_number=requested_pr_number,
        deadline_monotonic=deadline_monotonic,
    )

def recheck_original_c_processor_state(
    replay: OriginalCReplayEvidence, *, deadline_monotonic: float,
) -> Any:
    """Perform Native's sole post-reconstruction state read and bind it bytewise."""
    if (
        type(replay) is not OriginalCReplayEvidence
        or isinstance(deadline_monotonic, bool)
        or not isinstance(deadline_monotonic, (int, float))
        or not math.isfinite(deadline_monotonic)
        or float(deadline_monotonic) != replay.deadline_monotonic
        or deadline_monotonic <= time.monotonic()
    ):
        raise PromotionError("original C replay state reread deadline or context is invalid")
    reader = getattr(replay.native_resolver, "read_original_processor_state", None)
    require = getattr(replay.native_module, "require_verified_original_processor_state", None)
    if not callable(reader) or not callable(require):
        raise PromotionError("original B Native state reread is unavailable")
    try:
        later = reader(
            expected_facts=replay.facts, deadline_monotonic=deadline_monotonic,
        )
        if require(later) is not later:
            raise PromotionError("original B Native state reread is not sealed")
    except PromotionError:
        raise
    except Exception as exc:
        raise PromotionError("original B Native state reread failed closed") from exc
    for field in (
        "repository", "state_commit", "tree_sha", "generation_id", "index_path",
        "index_bytes", "index_sha256", "checkpoint_path", "checkpoint_bytes",
        "checkpoint_sha256",
    ):
        if getattr(replay.processor_state, field, None) != getattr(later, field, None):
            raise PromotionError("original B processor state changed during source replay")
    return later

def acquire_original_c_source_contract_facts(
    replay: OriginalCReplayEvidence,
    *,
    deadline_monotonic: float,
) -> Any:
    """Read the fixed-role source contract through the same Native resolver."""
    if (
        type(replay) is not OriginalCReplayEvidence
        or isinstance(deadline_monotonic, bool)
        or not isinstance(deadline_monotonic, (int, float))
        or not math.isfinite(deadline_monotonic)
        or float(deadline_monotonic) != replay.deadline_monotonic
        or deadline_monotonic <= time.monotonic()
        or replay.native_resolver is None
        or replay.native_module is not sys.modules.get(
            getattr(replay.native_module, "__name__", ""),
        )
    ):
        raise PromotionError("original C source-contract Native context is unavailable")
    method = getattr(replay.native_resolver, "read_original_source_contract_facts", None)
    require = getattr(
        replay.native_module,
        "require_verified_original_source_contract_facts",
        None,
    )
    if not callable(method) or not callable(require):
        raise PromotionError("original C source-contract Native reader is unavailable")
    try:
        facts = method(
            expected_facts=replay.facts,
            processor_state=replay.processor_state,
            processor_artifact=replay.processor_artifact,
            deadline_monotonic=deadline_monotonic,
        )
        if require(facts) is not facts:
            raise PromotionError("original C source-contract facts are not Native sealed")
        if getattr(facts, "eligibility_control", None) is not replay.control:
            raise PromotionError("original C source-contract facts changed CONTROL singleton")
    except PromotionError:
        raise
    except Exception as exc:
        raise PromotionError("original C source-contract acquisition failed closed") from exc
    return facts

def acquire_original_c_replay_control(
    replay: OriginalCReplayEvidence,
    source_facts: Any,
    *,
    deadline_monotonic: float,
) -> OriginalCReplayControlBinding:
    """Acquire Native's finite, source-proven current replay closure."""
    if (
        type(replay) is not OriginalCReplayEvidence
        or isinstance(deadline_monotonic, bool)
        or not isinstance(deadline_monotonic, (int, float))
        or not math.isfinite(deadline_monotonic)
        or float(deadline_monotonic) != replay.deadline_monotonic
        or deadline_monotonic <= time.monotonic()
    ):
        raise PromotionError("original C replay CONTROL deadline is invalid")
    resolver = replay.native_resolver
    native_module = replay.native_module
    read_control = getattr(resolver, "read_original_replay_control", None)
    require_control = getattr(native_module, "require_verified_original_replay_control", None)
    if not callable(read_control) or not callable(require_control):
        raise PromotionError("original C current replay CONTROL closure is unavailable")
    try:
        handle = read_control(
            expected_facts=replay.facts,
            source_facts=source_facts,
            deadline_monotonic=deadline_monotonic,
        )
        if require_control(handle, deadline_monotonic=deadline_monotonic) is not handle:
            raise PromotionError("original C replay CONTROL closure is not Native sealed")
        binding = OriginalCReplayControlBinding(
            _ORIGINAL_C_REPLAY_CONTROL_BINDING,
            replay=replay, source_facts=source_facts, native_control=handle,
        )
        binding.require(deadline_monotonic=deadline_monotonic)
    except PromotionError:
        raise
    except Exception as exc:
        raise PromotionError("original C current replay CONTROL closure failed closed") from exc
    return binding

def _original_c_control_data_sha256(
    control: OriginalCReplayControlBinding,
    relative_path: str,
    *,
    deadline_monotonic: float,
) -> str:
    """Return one exact raw-data digest from Native's closed execution inventory."""
    native_control = control.require(deadline_monotonic=deadline_monotonic)
    inventory = getattr(native_control, "inventory", None)
    if not isinstance(inventory, (list, tuple)):
        raise PromotionError("original C replay CONTROL inventory is unavailable")
    matches = [
        item for item in inventory
        if isinstance(item, Mapping) and item.get("path") == relative_path
    ]
    if len(matches) != 1:
        raise PromotionError("original C replay CONTROL data path is not uniquely inventoried")
    row = matches[0]
    digest = row.get("sha256")
    byte_count = row.get("bytes")
    if (
        not isinstance(digest, str) or not re.fullmatch(r"[a-f0-9]{64}", digest)
        or type(byte_count) is not int or byte_count < 1
        or not isinstance(row.get("kind"), str) or not row["kind"]
    ):
        raise PromotionError("original C replay CONTROL data inventory row is malformed")
    return digest

def _original_c_replay_modules(
    control: OriginalCReplayControlBinding,
    *,
    deadline_monotonic: float,
) -> dict[str, Any]:
    """Return only modules from the revalidated Native CONTROL capability."""
    if type(control) is not OriginalCReplayControlBinding:
        raise PromotionError("original C replay helper closure is not runner sealed")
    control.require(deadline_monotonic=deadline_monotonic)
    return {
        name: control.module(name, deadline_monotonic=deadline_monotonic)
        for name in (
            "eligibility", "handoff", "derivation", "preparer", "processor",
            "health", "canonical_update_pr", "composer", "link_detail",
            "seoul_declaration", "terminal_evidence",
        )
    }

def _native_original_source_role_inputs(
    source_facts: Any,
    eligibility: Any,
    checkpoint: Mapping[str, Any],
) -> dict[str, dict[str, Any]]:
    """Validate and project exact fixed-role Git blobs from a Native capability.

    This function consumes only the closed Native record shape. Its returned
    dictionaries are data for pure validators; they do not carry authority.
    """
    declared = getattr(eligibility, "CONTRACT_INPUTS", None)
    roles = getattr(source_facts, "roles", None)
    if not isinstance(declared, tuple) or not isinstance(roles, Mapping):
        raise PromotionError("original C source-contract role inventory is unavailable")
    expected_inputs: tuple[tuple[str, str], ...] = tuple(declared)
    if (
        not expected_inputs
        or any(
            not isinstance(row, tuple) or len(row) != 2
            or not isinstance(row[0], str) or not isinstance(row[1], str)
            for row in expected_inputs
        )
        or len({path for path, _effect in expected_inputs}) != len(expected_inputs)
        or tuple(tuple(row) for row in getattr(source_facts, "contract_inputs", ())) != expected_inputs
    ):
        raise PromotionError("original C source-contract inputs differ from the current CONTROL inventory")
    required_a_paths = tuple(
        path for path, effect in expected_inputs
        if effect == getattr(eligibility, "REQUIRES_NEW_A", None)
    )
    if tuple(getattr(source_facts, "requires_new_a_paths", ())) != required_a_paths:
        raise PromotionError("original C source-contract A-required paths differ from current CONTROL")
    input_provenance = getattr(source_facts, "processor_input_provenance", None)
    composition_inputs = getattr(source_facts, "processor_composition_inputs", None)
    if (
        not isinstance(input_provenance, Mapping)
        or not isinstance(composition_inputs, Mapping)
        or dict(input_provenance) != PROCESSOR_INPUT_PROVENANCE
        or dict(composition_inputs) != PROCESSOR_COMPOSITION_INPUTS
    ):
        raise PromotionError("original C source-contract processor lineage map differs from current CONTROL")
    compat_paths = tuple(getattr(source_facts, "processor_compatibility_paths", ()))
    expected_compat_paths = _processor_compatibility_source_paths(checkpoint)
    if (
        compat_paths != expected_compat_paths
        or len(compat_paths) != len(set(compat_paths))
        or any(path not in {item[0] for item in expected_inputs} for path in compat_paths)
    ):
        raise PromotionError("original C source-contract compatibility paths are open or invalid")

    generation_inputs = checkpoint.get("generation_inputs")
    if not isinstance(generation_inputs, Mapping):
        raise PromotionError("original C processor checkpoint lacks generation inputs")
    predecessor = generation_inputs.get("source_target_contract")
    expected_roles = {"processor_b", "original_m", "predecessor_target", "original_a"}
    if set(roles) != expected_roles:
        raise PromotionError("original C source-contract roles differ from the closed four-role set")
    all_paths = tuple(path for path, _effect in expected_inputs)
    role_paths = {
        "processor_b": all_paths,
        "original_m": all_paths,
        "predecessor_target": all_paths,
        "original_a": required_a_paths,
    }
    if predecessor is None:
        if roles.get("predecessor_target") is not None:
            raise PromotionError("legacy original C checkpoint has an unexpected predecessor target role")
    elif not isinstance(predecessor, Mapping) or not isinstance(roles.get("predecessor_target"), Mapping):
        raise PromotionError("modern original C checkpoint lacks its authenticated predecessor target role")
    input_positions = {
        path: (position, effect)
        for position, (path, effect) in enumerate(expected_inputs)
    }

    result: dict[str, dict[str, Any]] = {}
    for role, expected_paths in role_paths.items():
        record = roles.get(role)
        if record is None:
            if role == "predecessor_target" and predecessor is None:
                result[role] = {"commit_sha": None, "tree_sha": None, "content": {}}
                continue
            raise PromotionError(f"original C source-contract {role} role is missing")
        if not isinstance(record, Mapping) or set(record) != {"role", "commit_sha", "tree_sha", "paths"}:
            raise PromotionError(f"original C source-contract {role} role shape is invalid")
        if (
            record.get("role") != role
            or not isinstance(record.get("commit_sha"), str)
            or not re.fullmatch(r"[a-f0-9]{40}", record["commit_sha"])
            or not isinstance(record.get("tree_sha"), str)
            or not re.fullmatch(r"[a-f0-9]{40}", record["tree_sha"])
            or not isinstance(record.get("paths"), (list, tuple))
            or len(record["paths"]) != len(expected_paths)
        ):
            raise PromotionError(f"original C source-contract {role} role identity is invalid")
        contents: dict[str, bytes | None] = {}
        for row_index, path in enumerate(expected_paths):
            position, effect = input_positions[path]
            row = record["paths"][row_index]
            if not isinstance(row, Mapping) or set(row) != {
                "position", "path", "class", "present", "mode", "git_blob_sha1",
                "bytes", "sha256", "content",
            }:
                raise PromotionError(f"original C source-contract {role} path row is malformed")
            if row.get("position") != position or row.get("path") != path or row.get("class") != effect:
                raise PromotionError(f"original C source-contract {role} path order or class changed")
            if row.get("present") is False:
                if (
                    role != "original_a" or effect != getattr(eligibility, "REQUIRES_NEW_A", None)
                    or row.get("mode") is not None or row.get("git_blob_sha1") is not None
                    or row.get("bytes") != 0 or row.get("sha256") is not None
                    or row.get("content") is not None
                ):
                    raise PromotionError("original C source-contract has an unauthorized absent blob")
                contents[path] = None
                continue
            content = row.get("content")
            mode = row.get("mode")
            byte_count = row.get("bytes")
            git_oid = row.get("git_blob_sha1")
            digest = row.get("sha256")
            if (
                row.get("present") is not True
                or mode not in {"100644", "100755"}
                or not isinstance(content, bytes)
                or type(byte_count) is not int or byte_count != len(content)
                or not isinstance(git_oid, str) or not re.fullmatch(r"[a-f0-9]{40}", git_oid)
                or not isinstance(digest, str) or not re.fullmatch(r"[a-f0-9]{64}", digest)
                or hashlib.sha256(content).hexdigest() != digest
                or hashlib.sha1(b"blob " + str(len(content)).encode("ascii") + b"\0" + content).hexdigest() != git_oid
            ):
                raise PromotionError(f"original C source-contract {role} blob digest or mode is invalid")
            contents[path] = content
        result[role] = {
            "commit_sha": record["commit_sha"],
            "tree_sha": record["tree_sha"],
            "content": contents,
        }
    return result

def revalidate_original_c_source_target_contract(
    replay: OriginalCReplayEvidence,
    source_facts: Any,
) -> tuple[dict[str, Any], list[dict[str, Any]], dict[str, dict[str, Any]]]:
    """Bind Native role facts to the retained selection and recompute its target.

    This validates source identity and current-Control classification only. It
    does not create ``ResolvedSourceEligibility``; the caller must still
    reproduce the original candidate and pass it through the current
    eligibility module's native reread resolver before any productive use.
    """
    if type(replay) is not OriginalCReplayEvidence:
        raise PromotionError("original C replay acquisition is not runner sealed")
    native_module = replay.native_module
    require = getattr(native_module, "require_verified_original_source_contract_facts", None)
    if not callable(require):
        raise PromotionError("original C source-contract Native validator is unavailable")
    try:
        if require(source_facts) is not source_facts:
            raise PromotionError("original C source-contract facts are not Native sealed")
    except PromotionError:
        raise
    except Exception as exc:
        raise PromotionError("original C source-contract facts failed Native validation") from exc
    if (
        getattr(source_facts, "eligibility_control", None) is not replay.control
        or getattr(replay.control, "eligibility_module", None) is None
    ):
        raise PromotionError("original C source-contract facts changed their current CONTROL singleton")
    eligibility = replay.control.eligibility_module
    selection = getattr(replay.decoded, "selection", None)
    if (
        not isinstance(selection, Mapping)
        or selection.get("action") != "revalidate_c"
        or not isinstance(selection.get("evidence"), Mapping)
        or not isinstance(selection.get("evaluation"), Mapping)
    ):
        raise PromotionError("original C retained source selection is not a selected C action")
    try:
        evaluation = eligibility.validate_evaluation(selection["evaluation"])
    except Exception as exc:
        raise PromotionError("original C retained evaluation is invalid under current CONTROL") from exc
    if canonical_json(evaluation) != canonical_json(selection["evaluation"]):
        raise PromotionError("original C retained evaluation changed during validation")

    checkpoint = getattr(replay.processor_state, "checkpoint", None)
    run = getattr(replay.processor_artifact, "run", None)
    if not isinstance(checkpoint, Mapping) or not isinstance(run, Mapping):
        raise PromotionError("original C processor state or run evidence is unavailable")
    generation_inputs = checkpoint.get("generation_inputs")
    if not isinstance(generation_inputs, Mapping):
        raise PromotionError("original C processor checkpoint lacks generation inputs")
    evidence = selection["evidence"]
    original_observation = evidence.get("original_observation")
    selected_target = evidence.get("target_contract")
    if not isinstance(original_observation, Mapping) or not isinstance(selected_target, Mapping):
        raise PromotionError("original C selection lacks its retained observation or target contract")

    roles = _native_original_source_role_inputs(source_facts, eligibility, checkpoint)
    processor_b = roles["processor_b"]
    original_m = roles["original_m"]
    original_a = roles["original_a"]
    predecessor = roles["predecessor_target"]
    source_sha = evaluation.get("source_sha")
    source_tree_sha = evaluation.get("source_tree_sha")
    producer_head = run.get("head_sha")
    observation_head = original_observation.get("producer_head_sha")
    if (
        processor_b["commit_sha"] != producer_head
        or original_m["commit_sha"] != source_sha
        or original_m["tree_sha"] != source_tree_sha
        or original_a["commit_sha"] != observation_head
        or getattr(source_facts, "processor_is_ancestor", None) is not True
    ):
        raise PromotionError("original C B, A, and M source roles are not bound to the retained selection")
    preparation_base = getattr(source_facts, "preparation_base", None)
    if (
        not isinstance(preparation_base, Mapping)
        or preparation_base.get("commit_sha") != source_sha
        or preparation_base.get("tree_sha") != source_tree_sha
    ):
        raise PromotionError("original C preparation base differs from the retained M evaluation")
    stored_target = generation_inputs.get("source_target_contract")
    if stored_target is None:
        if predecessor["commit_sha"] is not None or predecessor["content"]:
            raise PromotionError("legacy original C checkpoint unexpectedly has predecessor-target facts")
        predecessor_tree_sha = None
        predecessor_bytes = None
    else:
        if not isinstance(stored_target, Mapping):
            raise PromotionError("original C stored predecessor target is malformed")
        if (
            predecessor["commit_sha"] != stored_target.get("source_sha")
            or predecessor["tree_sha"] != stored_target.get("source_tree_sha")
            or any(value is None for value in predecessor["content"].values())
        ):
            raise PromotionError("original C predecessor-target role differs from its stored contract")
        predecessor_tree_sha = predecessor["tree_sha"]
        predecessor_bytes = predecessor["content"]

    try:
        target_contract, inventory = target_contract_from_original_bounded_facts(
            eligibility,
            checkpoint,
            original_observation,
            current_source_sha=source_sha,
            current_source_tree_sha=source_tree_sha,
            current_input_bytes=original_m["content"],
            predecessor_source_tree_sha=predecessor_tree_sha,
            predecessor_input_bytes=predecessor_bytes,
            original_a_required_input_bytes=original_a["content"],
        )
    except PromotionError:
        raise
    except Exception as exc:
        raise PromotionError("original C retained target contract could not be recomputed") from exc
    try:
        selected = eligibility.validate_target_contract(selected_target)
    except Exception as exc:
        raise PromotionError("original C retained target contract is invalid under current CONTROL") from exc
    if canonical_json(target_contract) != canonical_json(selected):
        raise PromotionError("original C recomputed M target contract differs from the retained selection")
    return target_contract, inventory, roles

def validate_original_c_processor_source_compatibility(
    replay: OriginalCReplayEvidence,
    source_facts: Any,
    roles: Mapping[str, Mapping[str, Any]],
    *,
    composition_receipt: Mapping[str, Any] | None = None,
) -> None:
    """Apply the exact legacy or source-epoch B→M rule to Native role bytes."""
    if type(replay) is not OriginalCReplayEvidence:
        raise PromotionError("original C replay acquisition is not runner sealed")
    eligibility = getattr(replay.control, "eligibility_module", None)
    checkpoint = getattr(replay.processor_state, "checkpoint", None)
    run = getattr(replay.processor_artifact, "run", None)
    if not isinstance(checkpoint, Mapping) or not isinstance(run, Mapping):
        raise PromotionError("original C processor compatibility context is unavailable")
    if set(roles) != {"processor_b", "original_m", "predecessor_target", "original_a"}:
        raise PromotionError("original C processor compatibility roles are open")
    producer = roles["processor_b"]
    current = roles["original_m"]
    producer_sha = producer.get("commit_sha")
    current_sha = current.get("commit_sha")
    if (
        not isinstance(producer_sha, str)
        or not isinstance(current_sha, str)
        or producer_sha != run.get("head_sha")
        or getattr(source_facts, "processor_is_ancestor", None) is not True
    ):
        raise PromotionError("original C B→M source ancestry is not Native authenticated")
    producer_bytes = producer.get("content")
    current_bytes = current.get("content")
    if not isinstance(producer_bytes, Mapping) or not isinstance(current_bytes, Mapping):
        raise PromotionError("original C B→M source bytes are incomplete")
    generation_inputs = checkpoint.get("generation_inputs")
    if not isinstance(generation_inputs, Mapping):
        raise PromotionError("original C checkpoint lacks processor source lineage")
    source_epoch = generation_inputs.get("source_epoch")
    if source_epoch is None:
        compatibility_paths = _processor_compatibility_source_paths(checkpoint)
        if any(path not in producer_bytes or path not in current_bytes for path in compatibility_paths):
            raise PromotionError("original C legacy compatibility inputs are incomplete")
        _verify_processor_input_compatibility_from_bytes(
            checkpoint,
            producer_sha,
            current_sha,
            producer_bytes={path: producer_bytes[path] for path in compatibility_paths},
            current_bytes={path: current_bytes[path] for path in compatibility_paths},
            producer_is_ancestor=True,
            composition_receipt=composition_receipt,
        )
        return
    if eligibility is None:
        raise PromotionError("original C source-epoch current CONTROL module is unavailable")
    decoded_selection = getattr(replay.decoded, "selection", None)
    original_observation = (
        decoded_selection.get("evidence", {}).get("original_observation")
        if isinstance(decoded_selection, Mapping)
        and isinstance(decoded_selection.get("evidence"), Mapping)
        else None
    )
    evaluation = (
        decoded_selection.get("evaluation")
        if isinstance(decoded_selection, Mapping) else None
    )
    if not isinstance(original_observation, Mapping) or not isinstance(evaluation, Mapping):
        raise PromotionError("original C source epoch lacks retained A observation or M evaluation")
    if set(producer_bytes) != set(eligibility.CONTRACT_PATHS) or set(current_bytes) != set(eligibility.CONTRACT_PATHS):
        raise PromotionError("original C source epoch contract inputs are incomplete")
    try:
        evaluation = eligibility.validate_evaluation(evaluation)
    except Exception as exc:
        raise PromotionError("original C source epoch M evaluation is invalid") from exc
    if (
        evaluation.get("source_sha") != current_sha
        or evaluation.get("source_tree_sha") != current.get("tree_sha")
    ):
        raise PromotionError("original C source epoch role differs from the retained M evaluation")
    try:
        stored_target = eligibility.validate_target_contract(
            generation_inputs.get("source_target_contract"),
        )
        epoch = eligibility.validate_source_epoch(source_epoch)
    except Exception as exc:
        raise PromotionError("original C source-epoch provenance is invalid under current CONTROL") from exc
    if (
        stored_target.get("source_sha") != producer_sha
        or stored_target.get("source_tree_sha") != producer.get("tree_sha")
        or epoch.get("target_contract_sha256") != stored_target.get("contract_sha256")
        or epoch.get("original_observation_sha256")
        != hashlib.sha256(canonical_json(original_observation)).hexdigest()
    ):
        raise PromotionError("original C source epoch does not bind its exact B source and original A observation")
    producer_contract, producer_inventory = target_contract_from_bounded_source_bytes(
        eligibility,
        source_sha=producer_sha,
        source_tree_sha=producer["tree_sha"],
        input_bytes=producer_bytes,
    )
    if any(
        producer_contract[field] != stored_target[field]
        for field in (
            "schema_version", "source_sha", "source_tree_sha",
            "inventory_sha256", "contract_sha256",
        )
    ):
        raise PromotionError("original C stored target contract differs from exact B source bytes")
    current_contract, _current_inventory = target_contract_from_bounded_source_bytes(
        eligibility,
        source_sha=current_sha,
        source_tree_sha=current["tree_sha"],
        input_bytes=current_bytes,
        previous_inventory=producer_inventory,
    )
    changed = current_contract.get("changed_classes")
    if not isinstance(changed, list) or any(
        effect in changed
        for effect in (eligibility.SAME_A_PROCESSING, eligibility.REQUIRES_NEW_A)
    ):
        raise PromotionError("original C source epoch changed outside the C-only compatibility class")

def _original_c_strict_json(raw: bytes, *, label: str) -> Any:
    """Decode bounded replay JSON without accepting duplicate keys/constants."""
    def unique_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        value: dict[str, Any] = {}
        for key, item in pairs:
            if key in value:
                raise ValueError("duplicate JSON object key")
            value[key] = item
        return value

    def reject_constant(_value: str) -> Any:
        raise ValueError("non-finite JSON constant")

    try:
        return json.loads(
            raw.decode("utf-8"), object_pairs_hook=unique_pairs,
            parse_constant=reject_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise PromotionError(f"original C {label} JSON is invalid") from exc

def original_m_canonical_context_from_replay(
    replay: OriginalCReplayEvidence,
    *,
    root: pathlib.Path,
    checkpoint: Mapping[str, Any],
    source_facts: Any,
    bundle_dir: pathlib.Path,
) -> dict[str, Any]:
    """Join Native M identity to actual matching B registry bytes and rows.

    The returned shape is the existing pure validator context. It proves only
    that these exact B bytes match the registry digest declared by Native's
    sealed original-M Git manifest and pointer; it does not claim a provider
    read, publication, or current-pointer observation.
    """
    if type(replay) is not OriginalCReplayEvidence:
        raise PromotionError("original M canonical context lacks sealed C replay facts")
    require_parent = getattr(
        replay.native_module,
        "require_verified_original_canonical_parent_facts",
        None,
    )
    if not callable(require_parent):
        raise PromotionError("original M canonical identity validator is unavailable")
    try:
        parent_facts = require_parent(replay.parent_facts)
    except Exception as exc:
        raise PromotionError("original M canonical parent facts are not Native sealed") from exc
    if parent_facts is not replay.parent_facts:
        raise PromotionError("original M canonical parent facts changed identity")
    identity = getattr(parent_facts, "original_m_canonical_identity", None)
    expected_fields = {
        "repository", "purpose", "main_sha", "tree_sha", "manifest_path",
        "manifest_bytes", "manifest_sha256", "manifest_git_blob_sha1",
        "registry_path", "registry_bytes", "registry_sha256",
        "registry_representation", "registry_git_blob_sha1",
        "registry_git_blob_bytes", "registry_git_blob_sha256",
    }
    if not isinstance(identity, Mapping) or set(identity) != expected_fields:
        raise PromotionError("original M canonical identity is unavailable or open")
    decoded = getattr(replay.decoded, "selection", None)
    evaluation = decoded.get("evaluation") if isinstance(decoded, Mapping) else None
    roles = _native_original_source_role_inputs(
        source_facts,
        replay.control.eligibility_module,
        checkpoint,
    )
    original_m = roles["original_m"]
    repository = getattr(replay.facts, "repository", None)
    parent_base = getattr(parent_facts, "preparation_base", None)
    manifest_sha = identity.get("manifest_sha256")
    registry_bytes = identity.get("registry_bytes")
    registry_git_bytes = identity.get("registry_git_blob_bytes")
    registry_path = identity.get("registry_path")
    if (
        not isinstance(evaluation, Mapping)
        or not isinstance(repository, str) or not repository
        or not isinstance(identity.get("repository"), str)
        or identity["repository"].casefold() != repository.casefold()
        or identity.get("purpose") != "original-c-selection"
        or identity.get("main_sha") != evaluation.get("source_sha")
        or identity.get("tree_sha") != evaluation.get("source_tree_sha")
        or identity.get("main_sha") != original_m.get("commit_sha")
        or identity.get("tree_sha") != original_m.get("tree_sha")
        or identity.get("manifest_path") != "manifest.json"
        or identity.get("registry_path") != "data/data-go-kr.registry.json"
        or identity.get("registry_representation") != "git-lfs-pointer"
        or type(identity.get("manifest_bytes")) is not int or identity["manifest_bytes"] < 1
        or type(registry_bytes) is not int or registry_bytes < 1
        or type(registry_git_bytes) is not int or registry_git_bytes < 1
        or not isinstance(manifest_sha, str) or not re.fullmatch(r"[a-f0-9]{64}", manifest_sha)
        or not isinstance(identity.get("registry_sha256"), str)
        or not re.fullmatch(r"[a-f0-9]{64}", identity["registry_sha256"])
        or not isinstance(identity.get("manifest_git_blob_sha1"), str)
        or not re.fullmatch(r"[a-f0-9]{40}", identity["manifest_git_blob_sha1"])
        or not isinstance(identity.get("registry_git_blob_sha1"), str)
        or not re.fullmatch(r"[a-f0-9]{40}", identity["registry_git_blob_sha1"])
        or not isinstance(identity.get("registry_git_blob_sha256"), str)
        or not re.fullmatch(r"[a-f0-9]{64}", identity["registry_git_blob_sha256"])
        or not isinstance(registry_path, str)
        or pathlib.PurePosixPath(registry_path).is_absolute()
        or ".." in pathlib.PurePosixPath(registry_path).parts
        or "\\" in registry_path
        or not isinstance(parent_base, Mapping)
        or identity.get("main_sha") != parent_base.get("commit_sha")
        or identity.get("tree_sha") != parent_base.get("tree_sha")
    ):
        raise PromotionError("original M canonical identity differs from the retained evaluation")

    candidate_path = bundle_dir / "composed-candidate.registry.json"
    checkpoint_outputs = checkpoint.get("output_digests")
    matches = [
        row for row in checkpoint_outputs if isinstance(row, Mapping)
        and row.get("path") == "composed-candidate.registry.json"
    ] if isinstance(checkpoint_outputs, list) else []
    if len(matches) != 1 or candidate_path.is_symlink() or not candidate_path.is_file():
        raise PromotionError("original B archive lacks one regular composed registry payload")
    try:
        raw = candidate_path.read_bytes()
    except OSError as exc:
        raise PromotionError("original B composed registry payload is unavailable") from exc
    record = matches[0]
    digest = hashlib.sha256(raw).hexdigest()
    if (
        len(raw) != registry_bytes
        or digest != identity.get("registry_sha256")
        or record.get("bytes") != len(raw)
        or record.get("sha256") != digest
    ):
        raise PromotionError("original B composed registry bytes differ from Native M manifest and pointer")
    rows = _original_c_strict_json(raw, label="original B registry payload")
    if not isinstance(rows, list) or not rows:
        raise PromotionError("original M canonical payload is not a nonempty registry array")
    return {
        "identity": {
            "main_sha": identity["main_sha"],
            "tree_sha": identity["tree_sha"],
            "manifest_sha256": manifest_sha,
            "registry_path": registry_path,
            "registry_bytes": registry_bytes,
            "registry_sha256": identity["registry_sha256"],
        },
        "rows": rows,
    }

def _original_c_candidate_from_replay(
    replay: OriginalCReplayEvidence,
    source_facts: Any,
    modules: Mapping[str, Any],
    *,
    checkpoint: Mapping[str, Any],
    state: Any,
    source_target_contract: Mapping[str, Any],
    source_inventory: Sequence[Mapping[str, Any]],
    roles: Mapping[str, Mapping[str, Any]],
    canonical_parent_readback: Mapping[str, Any],
    bundle_dir: pathlib.Path,
) -> dict[str, Any]:
    """Rebuild the one retained C candidate from authenticated A/B state."""
    eligibility = modules.get("eligibility")
    handoff = modules.get("handoff")
    derivation = modules.get("derivation")
    preparer = modules.get("preparer")
    processor = modules.get("processor")
    if any(module is None for module in (eligibility, handoff, derivation, preparer, processor)):
        raise PromotionError("original C replay helper closure is incomplete")
    if checkpoint.get("status") not in {"ready", "no-change"}:
        raise PromotionError("original C retained B checkpoint is not reviewable")
    index = getattr(state, "index", None)
    index_bytes = getattr(state, "index_bytes", None)
    state_commit = getattr(state, "state_commit", None)
    index_path = getattr(state, "index_path", None)
    checkpoint_bytes = getattr(state, "checkpoint_bytes", None)
    checkpoint_path = getattr(state, "checkpoint_path", None)
    if (
        not isinstance(index, Mapping)
        or not isinstance(index_bytes, bytes)
        or not isinstance(state_commit, str) or not re.fullmatch(r"[a-f0-9]{40}", state_commit)
        or not isinstance(index_path, str) or not isinstance(checkpoint_path, str)
        or not isinstance(checkpoint_bytes, bytes)
        or canonical_json(index) != canonical_json(_original_c_strict_json(index_bytes, label="processor state index"))
        or canonical_json(checkpoint) != canonical_json(_original_c_strict_json(checkpoint_bytes, label="processor checkpoint"))
    ):
        raise PromotionError("original B state bytes differ from the Native sealed index or checkpoint")
    try:
        admission = preparer.admission_for_checkpoint(checkpoint, index, handoff)
        original = derivation.original_observation_from_checkpoint(checkpoint, admission)
        original_generation = eligibility.normalize_original_generation(checkpoint)
        source_epoch = eligibility.validate_source_epoch(
            checkpoint.get("generation_inputs", {}).get("source_epoch"),
        )
        readback = derivation.validate_parent_readback(canonical_parent_readback)
    except Exception as exc:
        raise PromotionError("original C retained A, epoch, or parent readback failed current CONTROL validation") from exc
    if original_generation.get("generation_id") != checkpoint.get("generation_id"):
        raise PromotionError("original C retained generation identity differs from B checkpoint")
    if source_epoch.get("target_generation_id") != checkpoint.get("generation_id"):
        raise PromotionError("original C retained source epoch differs from B generation")

    locator = checkpoint.get("output_artifact")
    if not isinstance(locator, Mapping):
        raise PromotionError("original C B checkpoint has no artifact locator")
    enrichment_path = bundle_dir / "upstream-catalogue-enrichment-evidence.json"
    if enrichment_path.is_symlink() or not enrichment_path.is_file():
        raise PromotionError("original B archive lacks its exact enrichment evidence")
    enrichment_bytes = enrichment_path.read_bytes()
    enrichment = _original_c_strict_json(enrichment_bytes, label="processor enrichment evidence")
    if not isinstance(enrichment, Mapping):
        raise PromotionError("original B enrichment evidence is not an object")
    try:
        processor_parent = derivation.processor_parent_reference(
            checkpoint,
            repository=str(getattr(replay.facts, "repository", "")),
            original_observation=original,
            enrichment_evidence=enrichment,
            enrichment_evidence_bytes=enrichment_bytes,
        )
    except Exception as exc:
        raise PromotionError("original B processor parent reference failed current CONTROL validation") from exc
    generation_inputs = checkpoint.get("generation_inputs")
    if not isinstance(generation_inputs, Mapping):
        raise PromotionError("original B checkpoint lacks generation inputs")
    stored_target = generation_inputs.get("source_target_contract")
    if stored_target is not None:
        try:
            stored_target = eligibility.validate_target_contract(stored_target)
        except Exception as exc:
            raise PromotionError("original B stored target contract is invalid") from exc
    output_rows = checkpoint.get("output_digests")
    if not isinstance(output_rows, list):
        raise PromotionError("original B checkpoint output inventory is invalid")
    artifact_run = str(locator.get("run_id") or "")
    artifact_name = str(locator.get("name") or "")
    attempt_match = re.fullmatch(r"upstream-catalogue-processing-[0-9]{6,20}-([1-9][0-9]*)", artifact_name)
    if not attempt_match:
        raise PromotionError("original B artifact attempt locator is malformed")
    accounting = {
        "checkpoint_ref": checkpoint.get("checkpoint_sha256"),
        "attempts_consumed": int(checkpoint.get("attempts_consumed", 0)),
        "attempts_by_id_sha256": eligibility.object_sha256(checkpoint.get("attempts_by_id", {})),
        "detail_queue_cursor": int(checkpoint.get("detail_queue_cursor", 0)),
        "detail_retry_reset_ids_sha256": eligibility.object_sha256(checkpoint.get("detail_retry_reset_ids", [])),
        "request_reservation_sha256": (
            eligibility.object_sha256(checkpoint["request_reservation"])
            if checkpoint.get("request_reservation") is not None else None
        ),
        "lease_sha256": (
            eligibility.object_sha256(checkpoint["lease"])
            if checkpoint.get("lease") is not None else None
        ),
        "fencing_token": int(checkpoint.get("fencing_token", 0)),
    }
    state_ref = {
        "repository": getattr(state, "repository", None),
        "commit_sha": state_commit,
        "path": index_path,
        "bytes": len(index_bytes),
        "sha256": hashlib.sha256(index_bytes).hexdigest(),
    }
    checkpoint_ref = {
        "repository": getattr(state, "repository", None),
        "commit_sha": state_commit,
        "path": checkpoint_path,
        "bytes": len(checkpoint_bytes),
        "sha256": hashlib.sha256(checkpoint_bytes).hexdigest(),
    }
    evidence_refs = [
        {"role": "admission_index", "kind": "git", **state_ref},
        {"role": "processor_checkpoint", "kind": "git", **checkpoint_ref},
        {
            "role": "processor_bundle", "repository": getattr(replay.facts, "repository", None),
            "kind": "actions", "run_id": artifact_run,
            "run_attempt": int(attempt_match.group(1)),
            "artifact_id": str(locator.get("artifact_id") or ""),
            "artifact_name": artifact_name,
            "bundle_manifest_sha256": str(locator.get("bundle_manifest_sha256") or ""),
            "output_bytes": sum(
                int(row.get("bytes", 0)) for row in output_rows if isinstance(row, Mapping)
            ),
        },
    ]
    admission_evidence = {
        **admission,
        "index_locator": {
            key: state_ref[key] for key in ("repository", "commit_sha", "path", "bytes", "sha256")
        },
    }
    evidence = {
        "original_observation": original,
        "admission": admission_evidence,
        "original_generation": original_generation,
        "processor_parent": processor_parent,
        "target_contract": dict(source_target_contract),
        "source_epoch": source_epoch,
        "accounting": accounting,
        "canonical_parent_readback": readback,
        "evidence_refs": evidence_refs,
    }
    return {
        "source_id": original["source_id"],
        "source_scope": original["source_scope"],
        "generation_id": checkpoint.get("generation_id"),
        "status": checkpoint.get("status"),
        "artifact_expires_at": admission.get("artifact_expires_at"),
        "accounting_exhausted": (
            int(checkpoint.get("attempts_consumed", 0)) >= processor.DEFAULT_MAX_ATTEMPTS
        ),
        "canonical_completed": False,
        "lease": checkpoint.get("lease"),
        "target_contract": stored_target,
        "original_observation": original,
        "evidence": evidence,
    }

def resolve_original_c_replay_eligibility(
    replay: OriginalCReplayEvidence,
    *,
    root: pathlib.Path,
) -> tuple[ResolvedSourceEligibility, Any]:
    """Replay the exact original selection, then perform Native's fresh join.

    This operation only constructs a runner eligibility capability after the
    original full selection has been recomputed from one exact A candidate,
    the admitted B artifact, and the original M target. The same Native
    resolver then re-reads the selected C identity and joins every preliminary
    fact before returning a revision capability.
    """
    if type(replay) is not OriginalCReplayEvidence:
        raise PromotionError("original C replay requires the exact runner acquisition capability")
    deadline = replay.deadline_monotonic
    if not math.isfinite(deadline) or deadline <= time.monotonic():
        raise PromotionError("original C replay deadline has expired")
    root = root.resolve(strict=True)
    source_facts = acquire_original_c_source_contract_facts(
        replay, deadline_monotonic=deadline,
    )
    replay_control = acquire_original_c_replay_control(
        replay, source_facts, deadline_monotonic=deadline,
    )
    modules = _original_c_replay_modules(
        replay_control, deadline_monotonic=deadline,
    )
    eligibility = modules["eligibility"]
    checkpoint_schema_value = replay_control.data(
        "checkpoint_schema", deadline_monotonic=deadline,
    )
    composition_schema = replay_control.data(
        "composition_schema", deadline_monotonic=deadline,
    )
    checkpoint_schema_path = root / "schemas/datapan.upstream-catalogue-checkpoint.v1.schema.json"
    try:
        checkpoint = verify_processor_checkpoint(
            dict(replay.processor_state.checkpoint),
            checkpoint_schema_path,
            eligibility_module=eligibility,
            schema_document=checkpoint_schema_value,
        )
    except Exception as exc:
        raise PromotionError("original B checkpoint failed current CONTROL validation") from exc
    if (
        checkpoint.get("source_id") != "data_go_kr"
        or checkpoint.get("source_scope") != "aggregate_supported_catalog"
        or checkpoint.get("generation_id") != replay.processor_state.generation_id
        or checkpoint.get("status") not in {"ready", "no-change"}
    ):
        raise PromotionError("original B checkpoint is outside the exact revalidation source or status")
    run_id, attempt, _artifact_name = processor_attempt_from_locator(checkpoint)
    run_value = getattr(replay.processor_artifact, "run", None)
    artifact_value = getattr(replay.processor_artifact, "artifact", None)
    archive = getattr(replay.processor_artifact, "archive_bytes", None)
    metadata_size = artifact_value.get("size_in_bytes") if isinstance(artifact_value, Mapping) else None
    if not isinstance(run_value, Mapping) or not isinstance(artifact_value, Mapping) or not isinstance(archive, bytes):
        raise PromotionError("original B attempt or immutable artifact bytes are unavailable")
    repository = str(getattr(replay.facts, "repository", ""))
    now = dt.datetime.now(dt.timezone.utc)
    try:
        trusted_run = validate_trusted_processor_run(
            run_value, repository=repository, run_id=run_id, attempt=attempt,
            default_branch="main", expected_head_sha=str(run_value.get("head_sha") or ""),
        )
        artifact = validate_processor_artifact_metadata(
            artifact_value, checkpoint, trusted_run, repository=repository, now=now,
        )
    except Exception as exc:
        raise PromotionError("original B run or artifact metadata failed current CONTROL validation") from exc
    if (
        type(metadata_size) is not int or len(archive) != metadata_size
        or len(archive) != getattr(replay.processor_artifact, "archive_byte_count", None)
        or hashlib.sha256(archive).hexdigest() != getattr(replay.processor_artifact, "archive_sha256", None)
    ):
        raise PromotionError("original B Native archive bytes differ from their exact artifact identity")

    with tempfile.TemporaryDirectory(prefix="datapan-original-c-replay-") as temporary:
        bundle_dir = pathlib.Path(temporary) / "bundle"
        try:
            extract_processor_artifact_archive_bytes(archive, artifact, bundle_dir)
        except Exception as exc:
            raise PromotionError("original B Native archive failed exact bounded extraction") from exc
        replay_control.require(deadline_monotonic=deadline)
        target_contract, target_inventory, roles = revalidate_original_c_source_target_contract(
            replay, source_facts,
        )
        if canonical_json(target_inventory) != canonical_json(
            eligibility.validate_inventory(list(target_inventory)),
        ):
            raise PromotionError("original C recomputed M inventory changed during validation")
        replay_control.require(deadline_monotonic=deadline)
        canonical_context = original_m_canonical_context_from_replay(
            replay, root=root, checkpoint=checkpoint, source_facts=source_facts,
            bundle_dir=bundle_dir,
        )
        bundle_path = bundle_dir / "composed-candidate.registry.json"
        raw_candidate = bundle_path.read_bytes()
        bundle_output_rows = checkpoint.get("output_digests")
        candidate_outputs = [
            row for row in bundle_output_rows if isinstance(row, Mapping)
            and row.get("path") == "composed-candidate.registry.json"
        ] if isinstance(bundle_output_rows, list) else []
        if len(candidate_outputs) != 1:
            raise PromotionError("original B output inventory lacks one composed registry")
        composer_path = "scripts/compose-upstream-catalogue-candidate.py"
        producer_composer = roles["processor_b"]["content"].get(composer_path)
        if not isinstance(producer_composer, bytes):
            raise PromotionError("original B source facts lack the exact producer composer")
        try:
            composition_helper = modules["canonical_update_pr"]
            bundle = validate_processor_bundle(
                checkpoint,
                bundle_dir,
                composition_schema,
                composition_helper,
                root=root,
                producer_composer_identity={
                    "bytes": len(producer_composer),
                    "sha256": hashlib.sha256(producer_composer).hexdigest(),
                },
                derivation_module=modules["derivation"],
                canonical_context=canonical_context,
                allow_terminal_noop=True,
                replay_control=replay_control,
                deadline_monotonic=deadline,
            )
            replay_control.require(deadline_monotonic=deadline)
            validate_original_c_processor_source_compatibility(
                replay, source_facts, roles,
                composition_receipt=bundle.get("composition_receipt"),
            )
            health_policy = replay_control.data("health_policy", deadline_monotonic=deadline)
            modules["preparer"].authenticate_original_native_canonical_parent(
                repository,
                checkpoint,
                canonical_context,
                health_policy,
                parent_facts=replay.parent_facts,
                native_module=replay.native_module,
                derivation=modules["derivation"],
                health=modules["health"],
                now=now,
            )
        except PromotionError:
            raise
        except Exception as exc:
            raise PromotionError("original C B/M/P semantic validation failed closed") from exc

        selection = getattr(replay.decoded, "selection", None)
        if not isinstance(selection, Mapping) or selection.get("action") != "revalidate_c":
            raise PromotionError("original C retained selection is not the original revalidate-C action")
        selection_evidence = selection.get("evidence")
        if not isinstance(selection_evidence, Mapping):
            raise PromotionError("original C retained selection has no closed evidence")
        candidate = _original_c_candidate_from_replay(
            replay,
            source_facts,
            modules,
            checkpoint=checkpoint,
            state=replay.processor_state,
            source_target_contract=target_contract,
            source_inventory=target_inventory,
            roles=roles,
            canonical_parent_readback=replay.parent_facts.parent_readback,
            bundle_dir=bundle_dir,
        )
        replay_control.require(deadline_monotonic=deadline)
        try:
            recomputed = eligibility.select_source_work(
                repository=repository,
                source_id=str(selection.get("source_id") or ""),
                source_scope=str(selection.get("source_scope") or ""),
                evaluation=selection.get("evaluation", {}),
                target_contract=target_contract,
                candidates=[candidate],
            )
        except Exception as exc:
            raise PromotionError("original C source selection could not be reproduced") from exc
        if canonical_json(recomputed) != canonical_json(selection):
            raise PromotionError("original C full source selection differs from exact retained comparison")
        facts_candidate = getattr(replay.facts, "selected_row", None)
        row_candidate = facts_candidate.get("candidate") if isinstance(facts_candidate, Mapping) else None
        source_target_binding = row_candidate.get("source_target") if isinstance(row_candidate, Mapping) else None
        try:
            checked_binding = modules["canonical_update_pr"].validate_source_target_binding(
                source_target_binding,
            )
        except Exception as exc:
            raise PromotionError("original C native source-target binding is invalid") from exc
        original_admission = selection_evidence.get("admission")
        original_observation = selection_evidence.get("original_observation")
        selected_contract = selection_evidence.get("target_contract")
        if (
            not isinstance(original_admission, Mapping)
            or not isinstance(original_observation, Mapping)
            or not isinstance(selected_contract, Mapping)
        ):
            raise PromotionError("original C selected source evidence is malformed")
        try:
            admission_reference = {
                "schema_version": modules["canonical_update_pr"].SOURCE_TARGET_ADMISSION_REF_SCHEMA,
                "generation_id": str(candidate["generation_id"]),
                "producer_run_id": str(original_admission.get("producer_run_id") or ""),
                "run_attempt": original_admission.get("run_attempt"),
                "head_sha": original_admission.get("head_sha"),
                "artifact_id": int(original_admission.get("artifact_id")),
                "artifact_digest_sha256": original_admission.get("artifact_digest_sha256"),
                "refresh_evidence_sha256": original_admission.get("refresh_evidence_sha256"),
            }
        except (TypeError, ValueError) as exc:
            raise PromotionError("original C selected A admission is malformed") from exc
        if (
            checked_binding.get("selection_key") != recomputed.get("selection_key")
            or checked_binding.get("original_observation_sha256")
            != hashlib.sha256(canonical_json(original_observation)).hexdigest()
            or checked_binding.get("target_contract_sha256") != selected_contract.get("contract_sha256")
            or checked_binding.get("original_admission_ref") != admission_reference
        ):
            raise PromotionError("original C selected row does not bind the exact recomputed A/M selection")

        def reread_candidates() -> list[Mapping[str, Any]]:
            replay_control.require(deadline_monotonic=deadline)
            state_again = recheck_original_c_processor_state(
                replay, deadline_monotonic=deadline,
            )
            checkpoint_again = getattr(state_again, "checkpoint", None)
            if not isinstance(checkpoint_again, Mapping) or canonical_json(checkpoint_again) != canonical_json(checkpoint):
                raise PromotionError("original B checkpoint changed before eligibility capability mint")
            source_facts_again = acquire_original_c_source_contract_facts(
                replay, deadline_monotonic=deadline,
            )
            if source_facts_again is not source_facts:
                raise PromotionError("original C Native source-contract facts changed during replay")
            replay_control.require(deadline_monotonic=deadline)
            return [
                _original_c_candidate_from_replay(
                    replay, source_facts, modules,
                    checkpoint=checkpoint_again, state=state_again,
                    source_target_contract=target_contract,
                    source_inventory=target_inventory,
                    roles=roles,
                    canonical_parent_readback=replay.parent_facts.parent_readback,
                    bundle_dir=bundle_dir,
                ),
            ]

        try:
            replay_control.require(deadline_monotonic=deadline)
            verified = eligibility.resolve_verified_source_eligibility(
                recomputed, reread_candidates=reread_candidates,
            )
        except Exception as exc:
            raise PromotionError("original C source eligibility Native reread failed") from exc
        runner_capability = ResolvedSourceEligibility(
            _RESOLVED_SOURCE_ELIGIBILITY,
            verified,
            {"checkpoint": checkpoint, "generation_id": checkpoint["generation_id"]},
        )
        resolve_final = getattr(replay.native_resolver, "resolve_owned_c_revision", None)
        require_final = getattr(replay.native_module, "require_verified_native_owned_c_revision", None)
        if not callable(resolve_final) or not callable(require_final):
            raise PromotionError("original C same-resolver final join is unavailable")
        try:
            replay_control.require(deadline_monotonic=deadline)
            final = resolve_final(
                verified_source_eligibility=runner_capability,
                journal_snapshot=replay.journal_snapshot,
                requested_pr_number=replay.requested_pr_number,
                deadline_monotonic=deadline,
                expected_facts=replay.facts,
                expected_ref=replay.expected_ref,
                expected_replay_control=replay_control.native_control,
            )
            if require_final(final) is not final:
                raise PromotionError("original C final Native join is not sealed")
        except PromotionError:
            raise
        except Exception as exc:
            raise PromotionError("original C same-resolver final join failed closed") from exc
        return runner_capability, final

class ValidatedSourceTargetNoopEvaluation:
    """Runner-owned proof that one exact source target screened to no C delta."""

    __slots__ = (
        "source_target", "source_eligibility", "evaluated_base_sha", "evaluator_identity",
        "terminal_outcome", "projection", "target_inventory",
    )

    def __init__(
        self,
        token: object,
        *,
        source_target: ResolvedSourceTarget,
        source_eligibility: ResolvedSourceEligibility,
        evaluated_base_sha: str,
        evaluator_identity: Mapping[str, Any],
        terminal_outcome: Mapping[str, Any],
        projection: Mapping[str, Any],
        target_inventory: Sequence[Mapping[str, Any]],
    ) -> None:
        if token is not _VALIDATED_SOURCE_TARGET_NOOP_EVALUATION:
            raise PromotionError("validated source-target no-op constructor is private")
        if type(source_target) is not ResolvedSourceTarget or type(source_eligibility) is not ResolvedSourceEligibility:
            raise PromotionError("validated source-target no-op lacks runner-owned capabilities")
        if not re.fullmatch(r"[a-f0-9]{40}", evaluated_base_sha):
            raise PromotionError("validated source-target no-op base is invalid")
        if source_target.evaluation.get("source_sha") != evaluated_base_sha:
            raise PromotionError("validated source-target no-op base differs from its source target")
        selection = source_eligibility.selection
        if (
            selection.get("action") != "revalidate_c"
            or selection.get("selection_key") != source_target.selection_key
            or source_eligibility.original_generation.get("generation_id") != source_target.generation_id
            or source_eligibility.target_contract != source_target.target_contract
        ):
            raise PromotionError("validated source-target no-op eligibility differs from its target")
        rows = terminal_outcome.get("generations")
        matching = [
            row for row in rows if isinstance(row, Mapping)
            and row.get("generation_id") == source_target.generation_id
            and row.get("status") == "already_canonical"
            and row.get("reason_code") == "source_contract_revalidated"
        ] if isinstance(rows, list) else []
        if (
            terminal_outcome.get("kind") != "recover-ready"
            or terminal_outcome.get("candidate_available") is not False
            or terminal_outcome.get("preparation_returned") is not False
            or terminal_outcome.get("selected_generation_id") is not None
            or len(matching) != 1
        ):
            raise PromotionError("validated source-target no-op terminal result is not a screened no-delta result")
        source_revalidation = source_eligibility.processor_candidate.get("source_revalidation")
        if (
            not isinstance(source_revalidation, Mapping)
            or projection != {
                "source_revalidation": dict(source_revalidation),
                "target_contract": source_target.target_contract,
            }
        ):
            raise PromotionError("validated source-target no-op projection differs from native screening")
        eligibility = load_module(
            pathlib.Path(__file__).resolve().parents[1] / "scripts/upstream_catalogue_source_eligibility.py",
            "validated_source_target_noop_inventory",
        )
        inventory = eligibility.validate_inventory(target_inventory)
        if inventory != source_target.target_inventory:
            raise PromotionError("validated source-target no-op inventory differs from its target")
        required_identity = {
            "repository", "workflow_path", "mode", "event", "run_id", "run_attempt",
            "source_sha", "source_tree_sha",
        }
        if set(evaluator_identity) != required_identity:
            raise PromotionError("validated source-target no-op evaluator identity is not closed")
        if (
            evaluator_identity.get("workflow_path") != TERMINAL_OUTCOME_WORKFLOW_PATH
            or evaluator_identity.get("mode") != "recover-ready"
            or evaluator_identity.get("source_sha") != evaluated_base_sha
            or evaluator_identity.get("source_tree_sha") != source_target.evaluation.get("source_tree_sha")
            or evaluator_identity.get("event") not in {"schedule", "workflow_run", "workflow_dispatch", "local"}
            or not re.fullmatch(r"[0-9]{1,20}", str(evaluator_identity.get("run_id", "")))
            or isinstance(evaluator_identity.get("run_attempt"), bool)
            or not isinstance(evaluator_identity.get("run_attempt"), int)
            or evaluator_identity.get("run_attempt", 0) < 1
        ):
            raise PromotionError("validated source-target no-op evaluator identity differs from the exact C invocation")
        self.source_target = source_target
        self.source_eligibility = source_eligibility
        self.evaluated_base_sha = evaluated_base_sha
        self.evaluator_identity = copy.deepcopy(dict(evaluator_identity))
        self.terminal_outcome = copy.deepcopy(dict(terminal_outcome))
        self.projection = copy.deepcopy(dict(projection))
        self.target_inventory = copy.deepcopy(inventory)

def source_target_selection_key(
    admission: Mapping[str, Any], target_contract: Mapping[str, Any],
) -> str:
    """Recompute a target key through the exact runner-owned selector source."""
    eligibility = load_module(
        pathlib.Path(__file__).resolve().parents[1]
        / "scripts/upstream_catalogue_source_eligibility.py",
        "trusted_c_source_target_key",
    )
    return str(eligibility.selection_key(admission, target_contract))

def target_contract_from_bounded_source_bytes(
    eligibility: Any,
    *,
    source_sha: str,
    source_tree_sha: str,
    input_bytes: Mapping[str, bytes],
    previous_inventory: Sequence[Mapping[str, Any]] | None = None,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Build the source target contract from already authenticated Git blobs.

    This is deliberately a pure semantic step. The caller must obtain the
    bytes and tree identity through the resolver's bounded, role-specific
    Native facts API; this helper never reads the checkout or starts Git.
    """
    if (
        not re.fullmatch(r"[a-f0-9]{40}", source_sha)
        or not re.fullmatch(r"[a-f0-9]{40}", source_tree_sha)
        or not isinstance(input_bytes, Mapping)
        or not isinstance(getattr(eligibility, "CONTRACT_INPUTS", None), tuple)
    ):
        raise PromotionError("original source target inputs are incomplete")
    declared = tuple(eligibility.CONTRACT_INPUTS)
    expected_paths = tuple(path for path, _effect in declared)
    if (
        len(expected_paths) != len(set(expected_paths))
        or set(input_bytes) != set(expected_paths)
        or any(not isinstance(input_bytes[path], bytes) for path in expected_paths)
    ):
        raise PromotionError("original source target inputs differ from the closed CONTROL inventory")
    raw_inventory = [
        {
            "position": position,
            "path": path,
            "class": effect,
            "bytes": len(input_bytes[path]),
            "sha256": hashlib.sha256(input_bytes[path]).hexdigest(),
        }
        for position, (path, effect) in enumerate(declared)
    ]
    try:
        inventory = eligibility.validate_inventory(raw_inventory)
        previous = (
            eligibility.validate_inventory(list(previous_inventory))
            if previous_inventory is not None else None
        )
        inventory_sha256 = eligibility.object_sha256(inventory)
        contract = eligibility.validate_target_contract({
            "schema_version": eligibility.TARGET_CONTRACT_SCHEMA_VERSION,
            "source_sha": source_sha,
            "source_tree_sha": source_tree_sha,
            "inventory_sha256": inventory_sha256,
            "contract_sha256": eligibility.target_contract_digest(inventory_sha256),
            "changed_classes": eligibility.changed_contract_classes(previous, inventory),
        })
    except Exception as exc:
        raise PromotionError("original source target contract failed current CONTROL validation") from exc
    return contract, inventory

def target_contract_from_original_bounded_facts(
    eligibility: Any,
    checkpoint: Mapping[str, Any],
    original_observation: Mapping[str, Any],
    *,
    current_source_sha: str,
    current_source_tree_sha: str,
    current_input_bytes: Mapping[str, bytes],
    predecessor_source_tree_sha: str | None = None,
    predecessor_input_bytes: Mapping[str, bytes] | None = None,
    original_a_required_input_bytes: Mapping[str, bytes | None] | None = None,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Recompute the retained target contract without selecting new work.

    Source maps are projections of one Native sealed fixed-role facts object,
    not caller-provided authority. A ``None`` A input is accepted only as an
    explicit authenticated absence emitted by that facts object.
    """
    generation_inputs = checkpoint.get("generation_inputs")
    if not isinstance(generation_inputs, Mapping):
        raise PromotionError("source selection generation inputs are invalid")
    current_contract, current_inventory = target_contract_from_bounded_source_bytes(
        eligibility,
        source_sha=current_source_sha,
        source_tree_sha=current_source_tree_sha,
        input_bytes=current_input_bytes,
    )
    stored_value = generation_inputs.get("source_target_contract")
    if stored_value is not None:
        try:
            prior_contract = eligibility.validate_target_contract(stored_value)
        except Exception as exc:
            raise PromotionError("stored source target contract is invalid") from exc
        prior_sha = prior_contract.get("source_sha")
        if (
            not isinstance(prior_sha, str)
            or predecessor_source_tree_sha is None
            or predecessor_input_bytes is None
        ):
            raise PromotionError("stored source target contract lacks its exact predecessor readback")
        recomputed_prior, prior_inventory = target_contract_from_bounded_source_bytes(
            eligibility,
            source_sha=prior_sha,
            source_tree_sha=predecessor_source_tree_sha,
            input_bytes=predecessor_input_bytes,
        )
        if any(
            recomputed_prior[field] != prior_contract[field]
            for field in (
                "schema_version", "source_sha", "source_tree_sha",
                "inventory_sha256", "contract_sha256",
            )
        ):
            raise PromotionError("stored source target contract differs from its exact predecessor readback")
        return target_contract_from_bounded_source_bytes(
            eligibility,
            source_sha=current_source_sha,
            source_tree_sha=current_source_tree_sha,
            input_bytes=current_input_bytes,
            previous_inventory=prior_inventory,
        )

    a_source_sha = original_observation.get("producer_head_sha")
    if not isinstance(a_source_sha, str) or not re.fullmatch(r"[a-f0-9]{40}", a_source_sha):
        raise PromotionError("original A source identity is malformed")
    required_paths = tuple(
        path for path, effect in eligibility.CONTRACT_INPUTS
        if effect == eligibility.REQUIRES_NEW_A
    )
    if (
        not isinstance(original_a_required_input_bytes, Mapping)
        or set(original_a_required_input_bytes) != set(required_paths)
        or any(
            value is not None and not isinstance(value, bytes)
            for value in original_a_required_input_bytes.values()
        )
    ):
        raise PromotionError("original A source-contract facts are incomplete or open")
    changed = any(
        original_a_required_input_bytes[path] is None
        or original_a_required_input_bytes[path] != current_input_bytes[path]
        for path in required_paths
    )
    try:
        contract = eligibility.validate_target_contract({
            **current_contract,
            "changed_classes": [
                eligibility.REQUIRES_NEW_A if changed else eligibility.SAME_A_PROCESSING
            ],
        })
    except Exception as exc:
        raise PromotionError("legacy original A target classification failed current CONTROL validation") from exc
    return contract, current_inventory

class _NoBytecodeSourceLoader(importlib.machinery.SourceFileLoader):
    """Load one explicit source path without writing import caches or toggling process globals."""

    def get_code(self, fullname: str) -> Any:
        source_path = self.get_filename(fullname)
        source = self.get_data(source_path)
        return self.source_to_code(source, source_path)

def _processor_compatibility_source_paths(checkpoint: Mapping[str, Any]) -> tuple[str, ...]:
    generation_inputs = checkpoint.get("generation_inputs")
    if not isinstance(generation_inputs, Mapping):
        raise PromotionError("processor checkpoint lacks generation input provenance")
    paths = list(PROCESSOR_COMPATIBILITY_FILES)
    if generation_inputs.get("same_observation_derivation") is not None:
        paths.append("scripts/upstream_catalogue_derivation.py")
    result = tuple(dict.fromkeys(paths))
    if not result or any(
        not isinstance(path, str)
        or not path
        or pathlib.PurePosixPath(path).is_absolute()
        or ".." in pathlib.PurePosixPath(path).parts
        or "\\" in path
        for path in result
    ):
        raise PromotionError("processor compatibility contract contains an unsafe source path")
    return result

def _verify_processor_input_compatibility_from_bytes(
    checkpoint: Mapping[str, Any],
    processor_head_sha: str,
    current_head_sha: str,
    *,
    producer_bytes: Mapping[str, bytes],
    current_bytes: Mapping[str, bytes],
    producer_is_ancestor: bool,
    composition_receipt: Mapping[str, Any] | None = None,
) -> None:
    """Apply the full legacy B→M compatibility contract to bounded readbacks.

    The original-C replay path supplies these exact maps from Native's sealed
    fixed-role source-facts capability. It must not pass a callback that can
    fall back to local Git, checkout files, or another transport.
    """
    if not re.fullmatch(r"[a-f0-9]{40}", processor_head_sha) or not re.fullmatch(r"[a-f0-9]{40}", current_head_sha):
        raise PromotionError("processor/current source commits must be full immutable Git SHAs")
    if producer_is_ancestor is not True:
        raise PromotionError("processor source commit is not an ancestor of the current trusted default branch")
    generation_inputs = checkpoint.get("generation_inputs")
    if not isinstance(generation_inputs, Mapping):
        raise PromotionError("processor checkpoint lacks generation input provenance")
    expected_paths = set(_processor_compatibility_source_paths(checkpoint))
    if (
        not isinstance(producer_bytes, Mapping)
        or not isinstance(current_bytes, Mapping)
        or set(producer_bytes) != expected_paths
        or set(current_bytes) != expected_paths
        or any(not isinstance(value, bytes) for value in producer_bytes.values())
        or any(not isinstance(value, bytes) for value in current_bytes.values())
    ):
        raise PromotionError("bounded processor compatibility facts are incomplete or open")
    historical_bytes: dict[str, bytes] = {}
    for raw_path in expected_paths:
        source_bytes = producer_bytes[raw_path]
        historical_bytes[raw_path] = source_bytes
        source_digest = hashlib.sha256(source_bytes).hexdigest()
        current_digest = hashlib.sha256(current_bytes[raw_path]).hexdigest()
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
    derivation_value = generation_inputs.get("same_observation_derivation")
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

def extract_processor_artifact_archive_bytes(
    raw: bytes,
    metadata: Mapping[str, Any],
    output_dir: pathlib.Path,
) -> pathlib.Path:
    """Materialize one already-acquired Native archive without network fallback."""
    if not isinstance(raw, bytes):
        raise ProcessorCandidateError("processor artifact archive is not an immutable byte string")
    size = metadata.get("size_in_bytes")
    if len(raw) != size:
        raise ProcessorCandidateError("downloaded processor artifact byte count differs from GitHub metadata")
    digest = metadata.get("digest")
    if isinstance(digest, str) and hashlib.sha256(raw).hexdigest() != digest.removeprefix("sha256:"):
        raise ProcessorCandidateError("downloaded processor archive digest differs from GitHub metadata")
    if output_dir.is_symlink():
        raise ProcessorCandidateError("processor artifact extraction destination is symbolic")
    output_dir = output_dir.absolute()
    if output_dir.exists():
        raise ProcessorCandidateError("processor artifact extraction destination already exists")
    output_dir.mkdir(parents=True, exist_ok=False)
    expected = set(REQUIRED_PROCESSOR_FILES) | {"upstream-catalogue-checkpoint-receipt.json"}
    total = 0
    try:
        with zipfile.ZipFile(io.BytesIO(raw)) as bundle:
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
                content = bundle.read(member)
                if len(content) != member.file_size:
                    raise ProcessorCandidateError("processor artifact member was truncated during extraction")
                with target.open("xb") as handle:
                    handle.write(content)
    except (OSError, zipfile.BadZipFile, RuntimeError) as exc:
        shutil.rmtree(output_dir, ignore_errors=True)
        raise ProcessorCandidateError("processor artifact is not a readable immutable ZIP bundle") from exc
    except BaseException:
        shutil.rmtree(output_dir, ignore_errors=True)
        raise
    return output_dir

def _release_closure_assessment_evidence(assessment: Mapping[str, Any]) -> tuple[Mapping[str, Any], Mapping[str, Any]]:
    """Return the closed reference and comparison record from either parent version."""
    modern = assessment.get("evidence")
    if isinstance(modern, Mapping):
        reference = modern.get("release_closure_reference")
        record = modern
    else:
        reference = assessment.get("reference")
        record = assessment.get("comparison")
    if not isinstance(reference, Mapping) or not isinstance(record, Mapping):
        raise PromotionError("release closure assessment lacks its versioned reference and comparison record")
    return reference, record

def _release_closure_comparison_path(reference: Mapping[str, Any]) -> str:
    key = reference.get("refresh_key")
    if not isinstance(key, str) or not re.fullmatch(r"[a-f0-9]{64}", key):
        raise PromotionError("release closure comparison reference has no full refresh key")
    return f"reports/data-go-kr/upstream-catalogue-review/release-closure/{key}/comparison.json"

def _check_release_closure_comparison_target(root: pathlib.Path, relative: str, *, must_be_absent: bool) -> pathlib.Path:
    pure = pathlib.PurePosixPath(relative)
    if pure.is_absolute() or not pure.parts or any(part in {"", ".", ".."} for part in pure.parts):
        raise PromotionError("release closure comparison path is unsafe")
    resolved_root = root.resolve(strict=True)
    target = resolved_root
    for index, part in enumerate(pure.parts):
        target = target / part
        if target.is_symlink():
            raise PromotionError("release closure comparison path traverses a symlink")
        if index < len(pure.parts) - 1 and target.exists() and not target.is_dir():
            raise PromotionError("release closure comparison parent is not a directory")
    if must_be_absent and target.exists():
        raise PromotionError("release closure comparison path already exists before the pre-receipt cut")
    return target

def _validate_release_closure_pre_receipt_cut(
    root: pathlib.Path,
    *,
    closure_helper: Any,
    reference: Mapping[str, Any],
    assessment_record: Mapping[str, Any],
) -> tuple[str, dict[str, Any]]:
    """Re-read G0 immediately before creating its sole post-cut comparison file."""
    comparison_path = _release_closure_comparison_path(reference)
    _check_release_closure_comparison_target(root, comparison_path, must_be_absent=True)
    base = assessment_record.get("current_preparation_base")
    if not isinstance(base, Mapping) or set(base) != {"commit_sha", "tree_sha"}:
        raise PromotionError("release closure pre-receipt cut has no exact M identity")
    current_head = command(("git", "rev-parse", "HEAD"), root).stdout.strip()
    current_tree = command(("git", "rev-parse", "HEAD^{tree}"), root).stdout.strip()
    if current_head != base.get("commit_sha") or current_tree != base.get("tree_sha"):
        raise PromotionError("release closure pre-receipt cut no longer uses its exact M commit/tree")
    captured = assessment_record.get("captured_changed_paths")
    if (
        not isinstance(captured, list)
        or any(not isinstance(path, str) or not path for path in captured)
        or captured != sorted(set(captured))
        or comparison_path in captured
        or changed_repository_paths(root) != captured
    ):
        raise PromotionError("release closure pre-receipt cut changed paths differ from the exact G0 readback")
    canonical_sha = assessment_record.get("canonical_sha256")
    original_observed_at = assessment_record.get("original_observed_at")
    rules_loader = getattr(closure_helper, "load_rules", None)
    projector = getattr(closure_helper, "project_tree", None)
    if not callable(rules_loader) or not callable(projector):
        raise PromotionError("release closure pre-receipt cut lacks its pinned project_tree implementation")
    try:
        projection = projector(
            root, canonical_sha256=canonical_sha,
            original_observed_at=original_observed_at, rules=rules_loader(),
        )
    except Exception as exc:  # noqa: BLE001 - unknown projection failures must stop before comparison creation
        raise PromotionError("release closure G0 projection could not be authenticated") from exc
    expected_bounds = assessment_record.get("projection_input_bounds")
    regenerated_bounds = expected_bounds.get("regenerated") if isinstance(expected_bounds, Mapping) else None
    actual_bounds = {
        "selected_bytes": projection.get("selected_bytes"),
        "raw_evidence_bytes": projection.get("raw_evidence_bytes"),
    }
    raw_paths = projection.get("raw_evidence_paths")
    if (
        not isinstance(regenerated_bounds, Mapping)
        or dict(regenerated_bounds) != actual_bounds
        or projection.get("projection_sha256") != assessment_record.get("regenerated_projection_sha256")
        or not isinstance(raw_paths, list)
        or comparison_path in raw_paths
    ):
        raise PromotionError("release closure G0 projection or raw evidence bounds differ from the assessment")
    return comparison_path, projection

def write_release_closure_comparison(
    root: pathlib.Path,
    *,
    closure_helper: Any,
    admitted_parent_ref: Mapping[str, Any],
    original_generation: Mapping[str, Any],
    source_eligibility_evidence: Mapping[str, Any],
    assessment: Mapping[str, Any],
    source_refresh_evidence_sha256: str,
    generator_consumer_closure_sha256: str,
    provenance_dependency_digests: Mapping[str, str],
) -> dict[str, Any]:
    """Persist the immutable positive pre-receipt comparison without self-links."""
    if assessment.get("status") != "eligible":
        raise PromotionError("release closure comparison artifact requires a positive pre-receipt assessment")
    reference, evidence = _release_closure_assessment_evidence(assessment)
    modern_parent = reference.get("schema_version") == "datapan.canonical-release-closure-refresh-reference.v2"
    if modern_parent:
        try:
            closure_helper.validate_admitted_parent_ref(admitted_parent_ref)
        except Exception as exc:  # noqa: BLE001 - native parent version must fail closed
            raise PromotionError("release closure parent reference failed exact versioned validation") from exc
    elif (
        reference.get("schema_version") != "datapan.canonical-release-closure-refresh-reference.v3"
        or not isinstance(admitted_parent_ref, Mapping)
        or set(admitted_parent_ref) != {"kind", "reference"}
        or admitted_parent_ref.get("kind") != "legacy_c_baseline"
        or not isinstance(admitted_parent_ref.get("reference"), Mapping)
    ):
        raise PromotionError("legacy release closure comparison lacks its exact tagged native baseline")
    try:
        derived_generator_closure = closure_helper.derive_generator_consumer_closure_sha256(
            provenance_dependency_digests,
        )
    except Exception as exc:  # noqa: BLE001 - reject open/incomplete membership before writing
        raise PromotionError("release closure dependencies do not match the complete reviewed membership") from exc
    if (
        original_generation.get("generation_id") != (
            admitted_parent_ref.get("generation_id") if modern_parent
            else admitted_parent_ref["reference"].get("identity", {}).get("generation_id")
        )
        or evidence.get("generation_id", original_generation.get("generation_id")) != original_generation.get("generation_id")
        or (modern_parent and evidence.get("admitted_parent_ref_sha256") != hashlib.sha256(
            canonical_json(dict(admitted_parent_ref))
        ).hexdigest())
        or (not modern_parent and reference.get("legacy_baseline_id") != admitted_parent_ref["reference"].get("baseline_id"))
        or (modern_parent and evidence.get("source_refresh_evidence_sha256") != source_refresh_evidence_sha256)
        or (not modern_parent and assessment.get("source_refresh_evidence_sha256") != source_refresh_evidence_sha256)
        or (modern_parent and evidence.get("generator_consumer_closure_sha256") != generator_consumer_closure_sha256)
        or (modern_parent and evidence.get("provenance_dependency_digests") != dict(provenance_dependency_digests))
        or (not modern_parent and assessment.get("generator_consumer_closure_sha256") != generator_consumer_closure_sha256)
        or (not modern_parent and assessment.get("provenance_dependency_digests") != dict(provenance_dependency_digests))
        or evidence.get("canonical_sha256") != reference.get("canonical_sha256")
        or derived_generator_closure != generator_consumer_closure_sha256
    ):
        raise PromotionError("release closure comparison changed its admitted parent, generation, or authenticated input evidence")
    key = reference.get("refresh_key")
    original_observed_at = evidence.get("original_observed_at")
    if not modern_parent:
        comparison_record = assessment.get("comparison")
        if not isinstance(comparison_record, Mapping):
            raise PromotionError("legacy release closure assessment lacks its comparison record")
        document = {
            "schema_version": "datapan.canonical-release-closure-comparison.v2",
            "purpose": "release-closure-refresh",
            "release_closure_reference": dict(reference),
            "parent_reference": dict(admitted_parent_ref),
            "original_generation": dict(original_generation),
            "source_eligibility_evidence": dict(source_eligibility_evidence),
            "source_refresh_evidence_sha256": source_refresh_evidence_sha256,
            "generator_consumer_closure_sha256": generator_consumer_closure_sha256,
            "provenance_dependency_digests": dict(provenance_dependency_digests),
            "comparison": comparison_record,
        }
    else:
        document = {
            "schema_version": "datapan.canonical-release-closure-comparison.v1",
            "purpose": "release-closure-refresh",
            "release_closure_reference": dict(reference),
            "admitted_parent_ref": dict(admitted_parent_ref),
            "original_generation": dict(original_generation),
            "source_eligibility_evidence": dict(source_eligibility_evidence),
            "source_refresh_evidence_sha256": source_refresh_evidence_sha256,
            "generator_consumer_closure_sha256": generator_consumer_closure_sha256,
            "provenance_dependency_digests": dict(provenance_dependency_digests),
            "comparison": {
                "ruleset_sha256": evidence.get("ruleset_sha256"),
                "canonical_sha256": evidence.get("canonical_sha256"),
                "canonical_representation": evidence.get("canonical_representation"),
                "canonical_git_blob_sha1": evidence.get("canonical_git_blob_sha1"),
                "canonical_git_blob_bytes": evidence.get("canonical_git_blob_bytes"),
                "canonical_git_blob_sha256": evidence.get("canonical_git_blob_sha256"),
                "admitted_parent_ref_sha256": evidence.get("admitted_parent_ref_sha256"),
                "current_preparation_base": evidence.get("current_preparation_base"),
                "admitted_parent_projection_sha256": evidence.get("admitted_parent_projection_sha256"),
                "current_base_projection_sha256": evidence.get("current_base_projection_sha256"),
                "regenerated_projection_sha256": evidence.get("regenerated_projection_sha256"),
                "parent_delta_paths": evidence.get("parent_delta_paths"),
                "current_base_delta_paths": evidence.get("current_base_delta_paths"),
                "parent_delta_records": evidence.get("parent_delta_records"),
                "current_base_delta_records": evidence.get("current_base_delta_records"),
                "current_base_delta_sha256": evidence.get("current_base_delta_sha256"),
                "captured_changed_paths": evidence.get("captured_changed_paths"),
                "projection_input_bounds": evidence.get("projection_input_bounds"),
                "source_eligibility_evidence_sha256": evidence.get("source_eligibility_evidence_sha256"),
                "original_generation_sha256": evidence.get("original_generation_sha256"),
                "original_generation_epoch": evidence.get("original_generation_epoch"),
                "original_epoch_origin": evidence.get("original_epoch_origin"),
                "original_observed_at": evidence.get("original_observed_at"),
            },
        }
    encoded = json.dumps(document, ensure_ascii=False, sort_keys=True, indent=2).encode("utf-8") + b"\n"
    maximum = getattr(closure_helper, "MAX_JSON_BYTES", 64 * 1024 * 1024)
    if len(encoded) > maximum:
        raise PromotionError("release closure comparison artifact exceeds the reviewed JSON evidence bound")
    relative = pathlib.PurePosixPath(_release_closure_comparison_path(reference))
    target = _check_release_closure_comparison_target(root, relative.as_posix(), must_be_absent=False)
    if target.exists():
        if target.is_symlink() or not target.is_file():
            raise PromotionError("release closure comparison path is unsafe")
        if not target.is_file() or target.read_bytes() != encoded:
            raise PromotionError("existing release closure comparison key is bound to different immutable evidence")
    else:
        _validate_release_closure_pre_receipt_cut(
            root, closure_helper=closure_helper, reference=reference,
            assessment_record=evidence,
        )
        target.parent.mkdir(parents=True, exist_ok=True)
        # Re-check path components after mkdir so a concurrent path substitution
        # cannot turn the immutable cut write into a symlink traversal.
        if _check_release_closure_comparison_target(root, relative.as_posix(), must_be_absent=True) != target:
            raise PromotionError("release closure comparison target changed during cut creation")
        fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
        with os.fdopen(fd, "wb") as stream:
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        directory_fd = os.open(target.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    return {"path": relative.as_posix(), "bytes": len(encoded), "sha256": hashlib.sha256(encoded).hexdigest()}

def _strict_json_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise PromotionError("release closure comparison contains a duplicate JSON key")
        result[key] = value
    return result

def _reject_nonfinite_comparison_value(value: str) -> None:
    raise PromotionError("release closure comparison contains a non-finite JSON number")

def _validate_release_closure_comparison_parent(
    candidate: Mapping[str, Any],
    reference: Mapping[str, Any],
    document: Mapping[str, Any],
    projection: Any,
) -> tuple[str, str, str, str | None]:
    """Validate the explicit modern-admission or legacy-baseline comparison wire."""
    ref_version = reference.get("schema_version")
    if ref_version == "datapan.canonical-release-closure-refresh-reference.v2":
        if (
            set(document) != RELEASE_CLOSURE_COMPARISON_TOP_FIELDS
            or document.get("schema_version") != "datapan.canonical-release-closure-comparison.v1"
            or not isinstance(document.get("admitted_parent_ref"), Mapping)
        ):
            raise PromotionError("modern release closure comparison has an unsupported or open shape")
        parent_ref = document["admitted_parent_ref"]
        try:
            parent_ref = projection.validate_admitted_parent_ref(parent_ref)
        except Exception as exc:  # noqa: BLE001 - the native admission-reference validator is authoritative
            raise PromotionError("modern comparison parent is not a supported exact native reference") from exc
        if (
            set(document.get("comparison", {})) != RELEASE_CLOSURE_COMPARISON_FIELDS
            or parent_ref.get("generation_id") != reference.get("generation_id")
            or parent_ref.get("canonical_sha256") != reference.get("canonical_sha256")
            or parent_ref.get("generation_id") != candidate.get("generation_id")
            or parent_ref.get("canonical_sha256") != candidate.get("registry_sha256")
        ):
            raise PromotionError("modern comparison parent identity differs from its release closure subject")
        return (
            parent_ref["generation_id"],
            parent_ref["canonical_sha256"],
            reference["admitted_parent_projection_sha256"],
            hashlib.sha256(canonical_json(dict(parent_ref))).hexdigest(),
        )

    if ref_version != "datapan.canonical-release-closure-refresh-reference.v3":
        raise PromotionError("release closure comparison reference uses an unsupported version")
    if (
        set(reference) != RELEASE_CLOSURE_LEGACY_REFERENCE_FIELDS
        or reference.get("purpose") != "release-closure-refresh"
        or set(document) != RELEASE_CLOSURE_COMPARISON_LEGACY_TOP_FIELDS
        or document.get("schema_version") != "datapan.canonical-release-closure-comparison.v2"
    ):
        raise PromotionError("legacy release closure comparison has an unsupported or open shape")
    for name in (
        "refresh_key", "legacy_baseline_id", "canonical_sha256",
        "legacy_parent_projection_sha256", "current_base_delta_sha256",
        "generated_projection_sha256",
    ):
        value = reference.get(name)
        if not isinstance(value, str) or not re.fullmatch(r"[a-f0-9]{64}", value):
            raise PromotionError(f"legacy release closure reference {name} is malformed")
    if not isinstance(reference.get("generation_id"), str) or not reference["generation_id"]:
        raise PromotionError("legacy release closure reference omits its original generation")
    branch = reference.get("branch")
    if not isinstance(branch, str) or not branch or ".." in branch or "//" in branch or "@{" in branch:
        raise PromotionError("legacy release closure reference branch is malformed")
    try:
        branch_prefix = projection.BASE._safe_branch_prefix(
            str(candidate.get("source_id", "")), str(candidate.get("scope", "")),
        )
    except Exception as exc:  # noqa: BLE001 - current CONTROL defines the only allowed branch namespace
        raise PromotionError("legacy release closure source namespace is unsupported") from exc
    suffix = branch[len(branch_prefix) + 1:] if branch.startswith(branch_prefix + "-") else ""
    if suffix not in {reference["refresh_key"][:width] for width in (12, 16, 20, 24, 32, 40, 48, 56, 64)}:
        raise PromotionError("legacy release closure branch is not bound to its exact full refresh key")

    parent_reference = document.get("parent_reference")
    if not isinstance(parent_reference, Mapping) or set(parent_reference) != RELEASE_CLOSURE_LEGACY_PARENT_FIELDS:
        raise PromotionError("legacy comparison parent is not the exact tagged native baseline")
    if parent_reference.get("kind") != "legacy_c_baseline":
        raise PromotionError("legacy comparison parent has an unsupported native capability kind")
    baseline = parent_reference.get("reference")
    if not isinstance(baseline, Mapping) or set(baseline) != RELEASE_CLOSURE_LEGACY_BASELINE_FIELDS:
        raise PromotionError("legacy comparison baseline has an unsupported or open shape")
    identity = baseline.get("identity")
    evidence = baseline.get("evidence")
    if (
        baseline.get("schema_version") != "datapan.canonical-release-legacy-c-baseline.v1"
        or baseline.get("role") != "legacy_c_comparison_baseline"
        or not isinstance(identity, Mapping)
        or set(identity) != RELEASE_CLOSURE_LEGACY_BASELINE_IDENTITY_FIELDS
        or not isinstance(evidence, Mapping)
        or set(evidence) != RELEASE_CLOSURE_LEGACY_BASELINE_EVIDENCE_FIELDS
    ):
        raise PromotionError("legacy comparison baseline does not preserve the exact native baseline record")
    baseline_id = baseline.get("baseline_id")
    expected_baseline_id = hashlib.sha256(
        b"datapan.canonical-release-legacy-c-baseline.v1\0" + canonical_json(dict(identity))
    ).hexdigest()
    if baseline_id != expected_baseline_id:
        raise PromotionError("legacy comparison baseline ID does not bind its immutable identity")
    if (
        identity.get("repository", "").casefold() != "statpan/datapan-registry"
        or identity.get("source_id") != candidate.get("source_id")
        or identity.get("scope") != candidate.get("scope")
        or identity.get("generation_id") != reference.get("generation_id")
        or identity.get("generation_id") != candidate.get("generation_id")
        or reference.get("legacy_baseline_id") != baseline_id
    ):
        raise PromotionError("legacy comparison baseline differs from the exact C subject")
    for name in ("candidate_head_sha", "candidate_base_sha", "merged_sha"):
        value = identity.get(name)
        if not isinstance(value, str) or not re.fullmatch(r"[a-f0-9]{40}", value):
            raise PromotionError(f"legacy baseline {name} is malformed")
    if (
        isinstance(identity.get("merged_pr_number"), bool)
        or not isinstance(identity.get("merged_pr_number"), int)
        or identity["merged_pr_number"] < 1
    ):
        raise PromotionError("legacy baseline merged PR number is malformed")
    ownership = identity.get("ownership")
    if (
        not isinstance(ownership, Mapping)
        or set(ownership) != {"owner_id", "branch", "body_sha256"}
        or not isinstance(ownership.get("branch"), str)
        or not ownership["branch"]
        or not isinstance(ownership.get("body_sha256"), str)
        or not re.fullmatch(r"[a-f0-9]{64}", ownership["body_sha256"])
    ):
        raise PromotionError("legacy baseline ownership identity is malformed")
    manifest = identity.get("release_manifest")
    canonical = identity.get("canonical_registry")
    git_representation = canonical.get("git_representation") if isinstance(canonical, Mapping) else None
    if (
        not isinstance(manifest, Mapping)
        or set(manifest) != {"path", "bytes", "sha256"}
        or manifest.get("path") != "manifest.json"
        or isinstance(manifest.get("bytes"), bool)
        or not isinstance(manifest.get("bytes"), int)
        or manifest["bytes"] < 1
        or not isinstance(manifest.get("sha256"), str)
        or not re.fullmatch(r"[a-f0-9]{64}", manifest["sha256"])
        or not isinstance(canonical, Mapping)
        or set(canonical) != {"path", "bytes", "sha256", "git_representation"}
        or canonical.get("path") != candidate.get("registry_path")
        or canonical.get("sha256") != candidate.get("registry_sha256")
        or isinstance(canonical.get("bytes"), bool)
        or not isinstance(canonical.get("bytes"), int)
        or canonical["bytes"] != candidate.get("registry_bytes")
        or not isinstance(git_representation, Mapping)
        or set(git_representation) != {
            "representation", "git_blob_sha1", "git_blob_bytes", "git_blob_sha256",
            "payload_bytes", "payload_sha256",
        }
        or git_representation.get("representation") not in {"git-lfs-pointer-v1", "materialized-git-blob-v1"}
        or not isinstance(git_representation.get("git_blob_sha1"), str)
        or not re.fullmatch(r"[a-f0-9]{40}", git_representation["git_blob_sha1"])
        or not isinstance(git_representation.get("git_blob_sha256"), str)
        or not re.fullmatch(r"[a-f0-9]{64}", git_representation["git_blob_sha256"])
        or isinstance(git_representation.get("git_blob_bytes"), bool)
        or not isinstance(git_representation.get("git_blob_bytes"), int)
        or git_representation["git_blob_bytes"] < 1
        or git_representation.get("payload_sha256") != canonical.get("sha256")
        or git_representation.get("payload_bytes") != canonical.get("bytes")
    ):
        raise PromotionError("legacy baseline manifest or canonical identity is malformed")
    comparison = document.get("comparison")
    if not isinstance(comparison, Mapping) or set(comparison) != RELEASE_CLOSURE_COMPARISON_LEGACY_FIELDS:
        raise PromotionError("legacy release closure comparison record has an unsupported or open shape")
    if (
        comparison.get("legacy_baseline_id") != baseline_id
        or comparison.get("legacy_parent_projection_sha256") != reference.get("legacy_parent_projection_sha256")
    ):
        raise PromotionError("legacy comparison changed its native baseline or parent projection identity")
    return (
        identity["generation_id"],
        canonical["sha256"],
        reference["legacy_parent_projection_sha256"],
        None,
    )

class _CurrentControlStateSourceLoader:
    """Execute only source bytes already matched to the executing CONTROL Git commit."""

    def __init__(self, source: bytes, *, filename: str, identity: tuple[str, ...]) -> None:
        self.source = source
        self.filename = filename
        self.identity = identity

    def create_module(self, spec: Any) -> None:
        return None

    def exec_module(self, module: types.ModuleType) -> None:
        code = compile(self.source, self.filename, "exec", dont_inherit=True)
        exec(code, module.__dict__)

def _check_absolute_deadline(deadline_monotonic: float, *, label: str) -> None:
    if (
        isinstance(deadline_monotonic, bool)
        or not isinstance(deadline_monotonic, (int, float))
        or not math.isfinite(deadline_monotonic)
        or time.monotonic() >= deadline_monotonic
    ):
        raise PromotionError(f"{label} deadline is unavailable")

def _ack_runtime_value(value: Any, *, seen: set[int], depth: int = 0) -> Any:
    """Describe the bounded runtime state reached by one ACK helper module."""
    if depth > _ACK_RUNTIME_MAX_DEPTH:
        raise PromotionError("current CONTROL runtime state is too deeply nested")
    if value is None or type(value) in {bool, int, str}:
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise PromotionError("current CONTROL runtime state contains a non-finite float")
        return value
    if isinstance(value, bytes):
        return {"kind": "bytes", "bytes": len(value), "sha256": hashlib.sha256(value).hexdigest()}
    if isinstance(value, pathlib.PurePath):
        return {"kind": "path", "value": value.as_posix()}
    if isinstance(value, re.Pattern):
        return {
            "kind": "pattern",
            "pattern": _ack_runtime_value(value.pattern, seen=seen, depth=depth + 1),
            "flags": value.flags,
        }
    identity = id(value)
    if identity in seen:
        return {"kind": "cycle", "identity": identity}
    if isinstance(value, Mapping):
        if len(value) > _ACK_RUNTIME_MAX_ITEMS:
            raise PromotionError("current CONTROL runtime mapping exceeds its bound")
        seen.add(identity)
        try:
            rows = [
                (
                    _ack_runtime_value(key, seen=seen, depth=depth + 1),
                    _ack_runtime_value(item, seen=seen, depth=depth + 1),
                )
                for key, item in value.items()
            ]
            rows.sort(key=lambda row: canonical_json(row[0]))
            return {"kind": "mapping", "rows": rows}
        finally:
            seen.remove(identity)
    if isinstance(value, (list, tuple)):
        if len(value) > _ACK_RUNTIME_MAX_ITEMS:
            raise PromotionError("current CONTROL runtime sequence exceeds its bound")
        seen.add(identity)
        try:
            return {
                "kind": type(value).__name__,
                "items": [
                    _ack_runtime_value(item, seen=seen, depth=depth + 1)
                    for item in value
                ],
            }
        finally:
            seen.remove(identity)
    if isinstance(value, (set, frozenset)):
        if len(value) > _ACK_RUNTIME_MAX_ITEMS:
            raise PromotionError("current CONTROL runtime set exceeds its bound")
        seen.add(identity)
        try:
            items = [
                _ack_runtime_value(item, seen=seen, depth=depth + 1)
                for item in value
            ]
            items.sort(key=canonical_json)
            return {"kind": type(value).__name__, "items": items}
        finally:
            seen.remove(identity)
    if isinstance(value, types.ModuleType):
        return {"kind": "module", "name": value.__name__, "identity": identity}
    if isinstance(value, property):
        return {
            "kind": "property", "identity": identity,
            "fget": _ack_runtime_value(value.fget, seen=seen, depth=depth + 1),
            "fset": _ack_runtime_value(value.fset, seen=seen, depth=depth + 1),
            "fdel": _ack_runtime_value(value.fdel, seen=seen, depth=depth + 1),
        }
    if isinstance(value, (types.MemberDescriptorType, types.GetSetDescriptorType)):
        owner = getattr(value, "__objclass__", None)
        return {
            "kind": type(value).__name__, "identity": identity,
            "name": getattr(value, "__name__", None),
            "owner_module": getattr(owner, "__module__", None),
            "owner_qualname": getattr(owner, "__qualname__", None),
        }
    if isinstance(value, types.FunctionType):
        closure = value.__closure__ or ()
        try:
            closure_values = [cell.cell_contents for cell in closure]
        except ValueError as exc:
            raise PromotionError("current CONTROL helper closure contains an empty cell") from exc
        return {
            "kind": "function", "identity": identity,
            "module": value.__module__, "qualname": value.__qualname__,
            "code_sha256": hashlib.sha256(marshal.dumps(value.__code__)).hexdigest(),
            "defaults": _ack_runtime_value(value.__defaults__, seen=seen, depth=depth + 1),
            "kwdefaults": _ack_runtime_value(value.__kwdefaults__, seen=seen, depth=depth + 1),
            "annotations": _ack_runtime_value(value.__annotations__, seen=seen, depth=depth + 1),
            "attributes": _ack_runtime_value(vars(value), seen=seen, depth=depth + 1),
            "closure": [
                _ack_runtime_value(item, seen=seen, depth=depth + 1)
                for item in closure_values
            ],
        }
    if isinstance(value, type):
        return {
            "kind": "class", "identity": identity,
            "module": value.__module__, "qualname": value.__qualname__,
        }
    try:
        attributes = vars(value)
    except TypeError:
        attributes = None
    return {
        "kind": "opaque", "identity": identity,
        "type_module": type(value).__module__, "type_name": type(value).__qualname__,
        "attributes": (
            _ack_runtime_value(attributes, seen=seen, depth=depth + 1)
            if isinstance(attributes, Mapping) else None
        ),
    }

def _ack_module_runtime_seal(module: Any) -> bytes:
    """Seal exact owned code, class descriptors, reached globals, and imports."""
    name = getattr(module, "__name__", None)
    if not isinstance(name, str) or not name or sys.modules.get(name) is not module:
        raise PromotionError("current CONTROL module singleton is not registered")
    namespace = vars(module)
    owned: dict[str, Any] = {}
    reached_globals: set[str] = set()
    for symbol, value in sorted(namespace.items()):
        if isinstance(value, types.FunctionType) and value.__module__ == name:
            owned[symbol] = _ack_runtime_value(value, seen=set())
            reached_globals.update(value.__code__.co_names)
        elif isinstance(value, type) and value.__module__ == name:
            class_state: dict[str, Any] = {}
            for method_name, method in sorted(vars(value).items()):
                if isinstance(method, (staticmethod, classmethod)):
                    target = method.__func__
                    class_state[method_name] = {
                        "descriptor": type(method).__name__,
                        "function": _ack_runtime_value(target, seen=set()),
                    }
                else:
                    class_state[method_name] = _ack_runtime_value(method, seen=set())
                    target = method
                targets = (target.fget, target.fset, target.fdel) if isinstance(target, property) else (target,)
                for function in targets:
                    if isinstance(function, types.FunctionType):
                        reached_globals.update(function.__code__.co_names)
            owned[symbol] = {
                "class": _ack_runtime_value(value, seen=set()),
                "mro": [_ack_runtime_value(base, seen=set()) for base in value.__mro__],
                "state": class_state,
            }
    globals_snapshot = {}
    for symbol in sorted(reached_globals | {key for key in namespace if key.isupper()} | {"__annotations__"}):
        if symbol in namespace:
            globals_snapshot[symbol] = _ack_runtime_value(namespace[symbol], seen=set())
    return canonical_json({"module": name, "owned": owned, "globals": globals_snapshot})

def _path_has_symlink_component(path: pathlib.Path) -> bool:
    absolute = path.absolute()
    current = pathlib.Path(absolute.anchor)
    for part in absolute.parts[1:]:
        current = current / part
        if current.is_symlink():
            return True
    return False

def _control_git_output(
    control_root: pathlib.Path,
    args: Sequence[str],
    *,
    deadline_monotonic: float,
    max_bytes: int,
) -> bytes:
    _check_absolute_deadline(deadline_monotonic, label="current CONTROL source read")
    if isinstance(max_bytes, bool) or not isinstance(max_bytes, int) or max_bytes < 1:
        raise PromotionError("current CONTROL Git output bound is invalid")
    command_args = tuple(args)
    allowed_paths = {
        "scripts/run-canonical-update-promotion.py",
        "scripts/canonical_publication_state.py",
        "scripts/canonical_update_pr.py",
        "scripts/canonical_release_closure.py",
        "scripts/canonical_release_closure_native_v2.py",
        *_CONTROL_SOURCE_MODULE_PATHS.values(),
        *_CONTROL_SOURCE_RESOURCE_PATHS,
    }
    identity_read = command_args == ("rev-parse", "--show-toplevel", "HEAD")
    blob_read = (
        len(command_args) == 3
        and command_args[0] == "cat-file"
        and command_args[1] in {"-s", "blob"}
        and isinstance(command_args[2], str)
        and ":" in command_args[2]
        and re.fullmatch(r"[a-f0-9]{40}", command_args[2].split(":", 1)[0]) is not None
        and command_args[2].split(":", 1)[1] in allowed_paths
    )
    if not (identity_read or blob_read):
        raise PromotionError("current CONTROL Git request is outside its fixed local read set")
    clean_env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    clean_env.update({
        "GIT_OPTIONAL_LOCKS": "0",
        "GIT_NO_REPLACE_OBJECTS": "1",
        "GIT_NO_LAZY_FETCH": "1",
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_SYSTEM": os.devnull,
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_ALLOW_PROTOCOL": "file",
    })
    remaining = deadline_monotonic - time.monotonic()
    if remaining <= 0:
        raise PromotionError("current CONTROL source read deadline expired")
    process: subprocess.Popen[bytes] | None = None
    selector: selectors.BaseSelector | None = None
    captured = bytearray()

    def reap_owned_child(child: subprocess.Popen[bytes]) -> bool:
        # Kill the whole owned session even if Git has already exited: a
        # descendant may still hold stdout open or continue after its parent.
        try:
            os.killpg(child.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        except OSError:
            return False
        term_deadline = time.monotonic() + 0.5
        while time.monotonic() < term_deadline:
            try:
                child.wait(timeout=min(0.05, max(0.0, term_deadline - time.monotonic())))
            except subprocess.TimeoutExpired:
                pass
            except OSError:
                return False
            try:
                os.killpg(child.pid, 0)
            except ProcessLookupError:
                return child.poll() is not None
            except OSError:
                return False
            time.sleep(0.01)
        try:
            os.killpg(child.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        except OSError:
            return False
        try:
            child.wait(timeout=0.5)
        except (OSError, subprocess.TimeoutExpired):
            if child.poll() is None:
                return False
        kill_deadline = time.monotonic() + 0.5
        while time.monotonic() < kill_deadline:
            try:
                os.killpg(child.pid, 0)
            except ProcessLookupError:
                return child.poll() is not None
            except OSError:
                return False
            time.sleep(0.01)
        return False

    try:
        process = subprocess.Popen(
            ("git", *command_args),
            cwd=control_root,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            env=clean_env,
            close_fds=True,
            start_new_session=True,
        )
        assert process.stdout is not None
        os.set_blocking(process.stdout.fileno(), False)
        selector = selectors.DefaultSelector()
        selector.register(process.stdout, selectors.EVENT_READ)
        while True:
            remaining = deadline_monotonic - time.monotonic()
            if remaining <= 0:
                if not reap_owned_child(process):
                    raise PromotionError("current CONTROL Git child cleanup could not be confirmed")
                raise PromotionError("current CONTROL Git identity read exceeded its absolute deadline")
            events = selector.select(timeout=min(remaining, 0.25))
            if not events:
                continue
            try:
                chunk = os.read(
                    process.stdout.fileno(),
                    min(65536, max_bytes - len(captured) + 1),
                )
            except BlockingIOError:
                continue
            if not chunk:
                selector.unregister(process.stdout)
                break
            captured.extend(chunk)
            if len(captured) > max_bytes:
                if not reap_owned_child(process):
                    raise PromotionError("current CONTROL Git child cleanup could not be confirmed")
                raise PromotionError("current CONTROL Git output exceeded its in-flight byte bound")
        remaining = deadline_monotonic - time.monotonic()
        if remaining <= 0:
            if not reap_owned_child(process):
                raise PromotionError("current CONTROL Git child cleanup could not be confirmed")
            raise PromotionError("current CONTROL Git identity read exceeded its absolute deadline")
        try:
            return_code = process.wait(timeout=remaining)
        except subprocess.TimeoutExpired as exc:
            if not reap_owned_child(process):
                raise PromotionError("current CONTROL Git child cleanup could not be confirmed") from exc
            raise PromotionError("current CONTROL Git identity read exceeded its absolute deadline") from exc
    except PromotionError:
        raise
    except (OSError, ValueError, selectors.SelectorError) as exc:
        if process is not None and not reap_owned_child(process):
            raise PromotionError("current CONTROL Git child cleanup could not be confirmed") from exc
        raise PromotionError("current CONTROL Git identity could not be read within its deadline") from exc
    finally:
        cleanup_failed = False
        if selector is not None:
            try:
                selector.close()
            except OSError:
                cleanup_failed = True
        if process is not None:
            if process.stdout is not None:
                try:
                    process.stdout.close()
                except OSError:
                    cleanup_failed = True
            if process.poll() is None and not reap_owned_child(process):
                raise PromotionError("current CONTROL Git child cleanup could not be confirmed")
            if process.poll() is not None:
                try:
                    os.killpg(process.pid, 0)
                except ProcessLookupError:
                    pass
                except OSError:
                    cleanup_failed = True
                else:
                    if not reap_owned_child(process):
                        raise PromotionError("current CONTROL Git descendant cleanup could not be confirmed")
                    cleanup_failed = True
        if cleanup_failed:
            raise PromotionError("current CONTROL Git process resources could not be closed")
    _check_absolute_deadline(deadline_monotonic, label="current CONTROL source read")
    if return_code != 0 or len(captured) > max_bytes:
        raise PromotionError("current CONTROL Git identity is missing or outside its byte bound")
    return bytes(captured)

def _read_current_control_blob(
    control_root: pathlib.Path,
    control_head_sha: str,
    relative_path: str,
    *,
    deadline_monotonic: float,
) -> bytes:
    if (
        not re.fullmatch(r"[a-f0-9]{40}", control_head_sha)
        or relative_path not in {
            "scripts/run-canonical-update-promotion.py",
            "scripts/canonical_publication_state.py",
            "scripts/canonical_update_pr.py",
            "scripts/canonical_release_closure.py",
            "scripts/canonical_release_closure_native_v2.py",
            *_CONTROL_SOURCE_RESOURCE_PATHS,
        }
    ):
        raise PromotionError("current CONTROL source identity is outside its fixed path set")
    spec = f"{control_head_sha}:{relative_path}"
    raw_size = _control_git_output(
        control_root, ("cat-file", "-s", spec),
        deadline_monotonic=deadline_monotonic, max_bytes=64,
    )
    try:
        size_text = raw_size.decode("ascii").strip()
        size = int(size_text)
    except (UnicodeDecodeError, ValueError) as exc:
        raise PromotionError("current CONTROL source blob size is invalid") from exc
    if size < 1 or size > MAX_CURRENT_CONTROL_SOURCE_BYTES:
        raise PromotionError("current CONTROL source blob exceeds its fixed size bound")
    raw = _control_git_output(
        control_root, ("cat-file", "blob", spec),
        deadline_monotonic=deadline_monotonic, max_bytes=MAX_CURRENT_CONTROL_SOURCE_BYTES,
    )
    if len(raw) != size:
        raise PromotionError("current CONTROL source blob changed while it was read")
    return raw

def _require_control_source_resource(
    control_root: pathlib.Path,
    control_head_sha: str,
    relative_path: str,
    *,
    deadline_monotonic: float,
) -> None:
    if relative_path not in _CONTROL_SOURCE_RESOURCE_PATHS:
        raise PromotionError("current CONTROL helper resource is outside its fixed path set")
    path = control_root / relative_path
    if _path_has_symlink_component(path):
        raise PromotionError("current CONTROL helper resource path contains a symlink")
    try:
        resolved = path.resolve(strict=True)
        if control_root not in resolved.parents or not resolved.is_file():
            raise PromotionError("current CONTROL helper resource is outside its checkout")
        size = resolved.stat().st_size
        if size < 1 or size > MAX_CURRENT_CONTROL_SOURCE_BYTES:
            raise PromotionError("current CONTROL helper resource exceeds its fixed byte bound")
        working_bytes = resolved.read_bytes()
    except OSError as exc:
        raise PromotionError("current CONTROL helper resource is unavailable") from exc
    committed_bytes = _read_current_control_blob(
        control_root, control_head_sha, relative_path,
        deadline_monotonic=deadline_monotonic,
    )
    if len(working_bytes) != size or working_bytes != committed_bytes:
        raise PromotionError("current CONTROL helper resource differs from its authenticated commit")
    resource_key = (str(resolved), control_head_sha, relative_path)
    baseline = _CONTROL_SOURCE_RESOURCE_BASELINES.get(resource_key)
    if baseline is None:
        _CONTROL_SOURCE_RESOURCE_BASELINES[resource_key] = bytes(committed_bytes)
    elif baseline != committed_bytes:
        raise PromotionError("current CONTROL helper resource changed after its authenticated baseline")

def _require_control_source_resource_runtime(
    control_root: pathlib.Path,
    control_head_sha: str,
    relative_path: str,
    *,
    deadline_monotonic: float,
) -> None:
    _check_absolute_deadline(deadline_monotonic, label="current CONTROL helper resource runtime check")
    if relative_path not in _CONTROL_SOURCE_RESOURCE_PATHS:
        raise PromotionError("current CONTROL helper resource is outside its fixed path set")
    path = control_root / relative_path
    if _path_has_symlink_component(path):
        raise PromotionError("current CONTROL helper resource path contains a symlink")
    try:
        resolved = path.resolve(strict=True)
        if resolved != path or control_root not in resolved.parents or not resolved.is_file():
            raise OSError("current CONTROL helper resource path changed")
        stat_result = resolved.stat()
        if stat_result.st_size < 1 or stat_result.st_size > MAX_CURRENT_CONTROL_SOURCE_BYTES:
            raise OSError("current CONTROL helper resource exceeds its byte bound")
        current_bytes = resolved.read_bytes()
    except (OSError, TypeError, ValueError) as exc:
        raise PromotionError("current CONTROL helper resource is unavailable") from exc
    key = (str(resolved), control_head_sha, relative_path)
    baseline = _CONTROL_SOURCE_RESOURCE_BASELINES.get(key)
    if (
        baseline is None
        or len(current_bytes) != stat_result.st_size
        or current_bytes != baseline
    ):
        raise PromotionError("current CONTROL helper resource changed after its authenticated baseline")
    _check_absolute_deadline(deadline_monotonic, label="current CONTROL helper resource runtime check")

def load_current_control_source_module(
    control_root: pathlib.Path,
    relative_path: str,
    *,
    module_name: str,
    state_module: types.ModuleType,
    deadline_monotonic: float,
) -> types.ModuleType:
    """Execute one allowlisted helper from the authenticated current CONTROL commit."""
    _check_absolute_deadline(deadline_monotonic, label="current CONTROL helper bootstrap")
    if _CONTROL_SOURCE_MODULE_PATHS.get(module_name) != relative_path:
        raise PromotionError("current CONTROL helper request is outside its fixed module map")
    if type(state_module) is not types.ModuleType or sys.modules.get("canonical_publication_state") is not state_module:
        raise PromotionError("current CONTROL helper requires the exact loaded State singleton")
    state_identity = getattr(state_module, "__datapan_current_control_state_identity__", None)
    if (
        not isinstance(state_identity, tuple)
        or len(state_identity) != 4
        or state_identity[0] != str(pathlib.Path(control_root).resolve(strict=True))
    ):
        raise PromotionError("current CONTROL helper lacks the authenticated runner/state identity")
    resolved_root = pathlib.Path(state_identity[0])
    control_head_sha = state_identity[1]
    source_path = resolved_root / relative_path
    if _path_has_symlink_component(source_path):
        raise PromotionError("current CONTROL helper source path contains a symlink")
    try:
        resolved_source = source_path.resolve(strict=True)
        size = resolved_source.stat().st_size
        if (
            resolved_source != source_path
            or resolved_root not in resolved_source.parents
            or not resolved_source.is_file()
            or size < 1
            or size > MAX_CURRENT_CONTROL_SOURCE_BYTES
        ):
            raise PromotionError("current CONTROL helper source is outside its fixed size/path bounds")
        working_bytes = resolved_source.read_bytes()
    except OSError as exc:
        raise PromotionError("current CONTROL helper source is unavailable") from exc
    source_bytes = _read_current_control_blob(
        resolved_root, control_head_sha, relative_path,
        deadline_monotonic=deadline_monotonic,
    )
    if len(working_bytes) != size or working_bytes != source_bytes:
        raise PromotionError("current CONTROL helper source differs from its authenticated commit")
    if relative_path == "scripts/canonical_release_closure.py":
        for resource_path in sorted(_CONTROL_SOURCE_RESOURCE_PATHS):
            _require_control_source_resource(
                resolved_root, control_head_sha, resource_path,
                deadline_monotonic=deadline_monotonic,
            )
    identity = (*state_identity, module_name, relative_path, hashlib.sha256(source_bytes).hexdigest())
    existing = sys.modules.get(module_name)
    known = _CONTROL_SOURCE_MODULES.get(module_name)
    if existing is not None:
        if (
            known is None
            or known[0] is not existing
            or known[1] != identity
            or getattr(existing, "__datapan_current_control_source_identity__", None) != identity
            or type(getattr(getattr(existing, "__spec__", None), "loader", None)) is not _CurrentControlStateSourceLoader
        ):
            raise PromotionError("current CONTROL helper module conflicts with its authenticated singleton")
        _require_current_control_source_module_runtime(
            existing, module_name=module_name, deadline_monotonic=deadline_monotonic,
        )
        return existing
    loader = _CurrentControlStateSourceLoader(
        source_bytes, filename=str(resolved_source), identity=identity,
    )
    spec = importlib.machinery.ModuleSpec(module_name, loader, origin=str(resolved_source))
    spec.has_location = True
    spec.cached = None
    module = importlib.util.module_from_spec(spec)
    module.__file__ = str(resolved_source)
    module.__cached__ = None
    sys.modules[module_name] = module
    try:
        _check_absolute_deadline(deadline_monotonic, label="current CONTROL helper bootstrap")
        loader.exec_module(module)
        module.__datapan_current_control_source_identity__ = identity
        runtime_baseline = _ack_module_runtime_seal(module)
        _CONTROL_SOURCE_MODULES[module_name] = (module, identity, runtime_baseline)
        _check_absolute_deadline(deadline_monotonic, label="current CONTROL helper runtime binding")
        return _require_current_control_source_module_runtime(
            module, module_name=module_name, deadline_monotonic=deadline_monotonic,
        )
    except BaseException:
        sys.modules.pop(module_name, None)
        _CONTROL_SOURCE_MODULES.pop(module_name, None)
        raise

def _require_current_control_source_module_runtime(
    module: Any,
    *,
    module_name: str,
    deadline_monotonic: float,
) -> types.ModuleType:
    _check_absolute_deadline(deadline_monotonic, label="current CONTROL helper runtime check")
    relative_path = _CONTROL_SOURCE_MODULE_PATHS.get(module_name)
    if relative_path is None or type(module) is not types.ModuleType:
        raise PromotionError("current CONTROL helper runtime request is outside its fixed module map")
    known = _CONTROL_SOURCE_MODULES.get(module_name)
    state = sys.modules.get("canonical_publication_state")
    state_identity = getattr(state, "__datapan_current_control_state_identity__", None)
    if (
        known is None
        or len(known) != 3
        or known[0] is not module
        or getattr(module, "__name__", None) != module_name
        or sys.modules.get(module_name) is not module
        or not isinstance(state_identity, tuple)
        or len(state_identity) != 4
    ):
        raise PromotionError("current CONTROL helper runtime baseline is unavailable")
    identity = known[1]
    expected_identity_prefix = (*state_identity, module_name, relative_path)
    if (
        identity[:-1] != expected_identity_prefix
        or getattr(module, "__datapan_current_control_source_identity__", None) != identity
        or type(getattr(getattr(module, "__spec__", None), "loader", None)) is not _CurrentControlStateSourceLoader
    ):
        raise PromotionError("current CONTROL helper source binding changed after execution")
    expected_path = pathlib.Path(state_identity[0]) / relative_path
    try:
        if (
            _path_has_symlink_component(expected_path)
            or expected_path.resolve(strict=True) != expected_path
            or pathlib.Path(getattr(module, "__file__", None)).resolve(strict=True) != expected_path
        ):
            raise OSError("current CONTROL helper source path drifted")
        stat_result = expected_path.stat()
        if stat_result.st_size < 1 or stat_result.st_size > MAX_CURRENT_CONTROL_SOURCE_BYTES:
            raise OSError("current CONTROL helper source exceeds its byte bound")
        current_bytes = expected_path.read_bytes()
    except (OSError, TypeError, ValueError) as exc:
        raise PromotionError("current CONTROL helper source path is unavailable") from exc
    if (
        len(current_bytes) != stat_result.st_size
        or hashlib.sha256(current_bytes).hexdigest() != identity[-1]
        or _ack_module_runtime_seal(module) != known[2]
    ):
        raise PromotionError("current CONTROL helper source or runtime changed after authentication")
    _check_absolute_deadline(deadline_monotonic, label="current CONTROL helper runtime check")
    return module

class _CurrentControlAckModules:
    __slots__ = (
        "state", "canonical_update_pr", "closure", "projection",
        "source_eligibility", "derivation", "deadline_monotonic", "_seal",
    )

    def __init__(
        self,
        seal: object,
        *,
        state: types.ModuleType,
        canonical_update_pr: types.ModuleType,
        closure: types.ModuleType,
        projection: types.ModuleType,
        source_eligibility: types.ModuleType | None = None,
        derivation: types.ModuleType | None = None,
        deadline_monotonic: float,
    ) -> None:
        if seal is not _CONTROL_ACK_MODULES_SEAL:
            raise PromotionError("current CONTROL ACK module bundle construction is private")
        _check_absolute_deadline(deadline_monotonic, label="current CONTROL ACK bundle construction")
        if source_eligibility is None or derivation is None:
            raise PromotionError("current CONTROL ACK bundle lacks source eligibility or derivation")
        self.state = state
        self.canonical_update_pr = canonical_update_pr
        self.closure = closure
        self.projection = projection
        self.source_eligibility = source_eligibility
        self.derivation = derivation
        self.deadline_monotonic = float(deadline_monotonic)
        self._seal = seal

def load_current_control_ack_modules(
    control_root: pathlib.Path,
    *,
    deadline_monotonic: float,
) -> _CurrentControlAckModules:
    """Load the fixed ACK helper set after binding it to the executing CONTROL commit."""
    deadline = deadline_monotonic
    _check_absolute_deadline(deadline, label="current CONTROL ACK bootstrap")
    state = load_current_control_publication_state(
        control_root, deadline_monotonic=deadline,
    )
    helper = load_current_control_source_module(
        control_root, "scripts/canonical_update_pr.py",
        module_name="canonical_update_pr_release_closure_ack",
        state_module=state, deadline_monotonic=deadline,
    )
    closure = load_current_control_source_module(
        control_root, "scripts/canonical_release_closure.py",
        module_name="canonical_release_closure_frozen_v3",
        state_module=state, deadline_monotonic=deadline,
    )
    projection = load_current_control_source_module(
        control_root, "scripts/canonical_release_closure_native_v2.py",
        module_name="canonical_release_closure_native_v2_ack",
        state_module=state, deadline_monotonic=deadline,
    )
    source_eligibility = load_current_control_source_module(
        control_root, "scripts/upstream_catalogue_source_eligibility.py",
        module_name="upstream_catalogue_source_eligibility_c2_ack",
        state_module=state, deadline_monotonic=deadline,
    )
    derivation = load_current_control_source_module(
        control_root, "scripts/upstream_catalogue_derivation.py",
        module_name="upstream_catalogue_derivation_c2_ack",
        state_module=state, deadline_monotonic=deadline,
    )
    if getattr(projection, "BASE", None) is not closure:
        raise PromotionError("current CONTROL release-closure adapter did not reuse its authenticated base singleton")
    modules = _CurrentControlAckModules(
        _CONTROL_ACK_MODULES_SEAL,
        state=state, canonical_update_pr=helper, closure=closure, projection=projection,
        source_eligibility=source_eligibility, derivation=derivation,
        deadline_monotonic=deadline,
    )
    return _require_current_control_ack_modules(modules, deadline_monotonic=deadline)

def _require_current_control_ack_modules(
    value: Any,
    *,
    deadline_monotonic: float,
) -> _CurrentControlAckModules:
    if type(value) is not _CurrentControlAckModules or value._seal is not _CONTROL_ACK_MODULES_SEAL:
        raise PromotionError("publication ACK helper bundle is not the exact current CONTROL load")
    _check_absolute_deadline(deadline_monotonic, label="current CONTROL ACK bundle validation")
    if (
        isinstance(deadline_monotonic, bool)
        or not isinstance(deadline_monotonic, (int, float))
        or float(deadline_monotonic) != value.deadline_monotonic
        or time.monotonic() >= value.deadline_monotonic
    ):
        raise PromotionError("publication ACK helper bundle has a different or expired deadline")
    expected = {
        "canonical_publication_state": value.state,
        "canonical_update_pr_release_closure_ack": value.canonical_update_pr,
        "canonical_release_closure_frozen_v3": value.closure,
        "canonical_release_closure_native_v2_ack": value.projection,
        "upstream_catalogue_source_eligibility_c2_ack": value.source_eligibility,
        "upstream_catalogue_derivation_c2_ack": value.derivation,
    }
    if any(
        not isinstance(module, types.ModuleType)
        or sys.modules.get(name) is not module
        for name, module in expected.items()
    ):
        raise PromotionError("publication ACK helper singleton changed after current CONTROL binding")
    if getattr(value.projection, "BASE", None) is not value.closure:
        raise PromotionError("publication ACK adapter no longer uses its authenticated CONTROL base")
    state_identity = getattr(value.state, "__datapan_current_control_state_identity__", None)
    if not isinstance(state_identity, tuple) or len(state_identity) != 4:
        raise PromotionError("publication ACK state identity is unavailable")
    root = pathlib.Path(state_identity[0])
    state_path = root / "scripts/canonical_publication_state.py"
    _require_current_control_state_module(
        value.state,
        state_path=state_path,
        identity=state_identity,
        deadline_monotonic=deadline_monotonic,
    )
    for module, name in (
        (value.canonical_update_pr, "canonical_update_pr_release_closure_ack"),
        (value.closure, "canonical_release_closure_frozen_v3"),
        (value.projection, "canonical_release_closure_native_v2_ack"),
        (value.source_eligibility, "upstream_catalogue_source_eligibility_c2_ack"),
        (value.derivation, "upstream_catalogue_derivation_c2_ack"),
    ):
        _require_current_control_source_module_runtime(
            module, module_name=name, deadline_monotonic=deadline_monotonic,
        )
    for relative_path in sorted(_CONTROL_SOURCE_RESOURCE_PATHS):
        _require_control_source_resource_runtime(
            root, state_identity[1], relative_path,
            deadline_monotonic=deadline_monotonic,
        )
    _check_absolute_deadline(deadline_monotonic, label="current CONTROL ACK bundle validation")
    return value

def _require_current_control_state_module(
    module: Any,
    *,
    state_path: pathlib.Path,
    identity: tuple[str, ...],
    deadline_monotonic: float,
) -> types.ModuleType:
    _check_absolute_deadline(deadline_monotonic, label="current CONTROL state validation")
    if type(module) is not types.ModuleType or getattr(module, "__name__", None) != "canonical_publication_state":
        raise PromotionError("current CONTROL Git state singleton has a conflicting module type or name")
    try:
        module_path = pathlib.Path(getattr(module, "__file__", None)).resolve(strict=True)
        expected_path = state_path.resolve(strict=True)
        spec = getattr(module, "__spec__", None)
        spec_origin = pathlib.Path(getattr(spec, "origin", None)).resolve(strict=True)
    except (OSError, TypeError, ValueError) as exc:
        raise PromotionError("current CONTROL Git state singleton has no exact source origin") from exc
    if (
        module_path != expected_path
        or spec_origin != expected_path
        or _path_has_symlink_component(state_path)
    ):
        raise PromotionError("current CONTROL Git state singleton is outside its exact source path")
    loader = getattr(spec, "loader", None)
    if (
        type(loader) is not _CurrentControlStateSourceLoader
        or loader.filename != str(expected_path)
        or loader.identity != identity
    ):
        raise PromotionError("current CONTROL Git state singleton has an unsupported loader")
    marker = getattr(module, "__datapan_current_control_state_identity__", None)
    known = _CURRENT_CONTROL_STATE_MODULES.get(str(expected_path))
    if marker != identity:
        raise PromotionError("current CONTROL Git state singleton identity changed")
    if known is not None and (
        known[0] is not module or known[1] != identity or len(known) != 3
    ):
        raise PromotionError("current CONTROL Git state singleton conflicts with the validated process identity")
    for name in ("GitObjectStore", "PythonModuleIndex", "PythonModuleIndexEntry"):
        value = getattr(module, name, None)
        if (
            not isinstance(value, type)
            or value.__module__ != "canonical_publication_state"
            or sys.modules.get("canonical_publication_state") is not module
        ):
            raise PromotionError("current CONTROL Git state singleton has mismatched native type bindings")
    runner_path = pathlib.Path(identity[0]) / "scripts/run-canonical-update-promotion.py"
    try:
        if _path_has_symlink_component(runner_path) or _path_has_symlink_component(state_path):
            raise OSError("current CONTROL source path contains a symlink")
        runner_resolved = runner_path.resolve(strict=True)
        state_resolved = state_path.resolve(strict=True)
        runner_stat = runner_resolved.stat()
        state_stat = state_resolved.stat()
        if (
            runner_resolved != runner_path
            or state_resolved != expected_path
            or runner_stat.st_size < 1
            or runner_stat.st_size > MAX_CURRENT_CONTROL_SOURCE_BYTES
            or state_stat.st_size < 1
            or state_stat.st_size > MAX_CURRENT_CONTROL_SOURCE_BYTES
        ):
            raise OSError("current CONTROL source exceeds its path or size bound")
        runner_bytes = runner_resolved.read_bytes()
        state_bytes = state_resolved.read_bytes()
    except (OSError, TypeError, ValueError) as exc:
        raise PromotionError("current CONTROL source changed or became unavailable") from exc
    if (
        len(runner_bytes) != runner_stat.st_size
        or len(state_bytes) != state_stat.st_size
        or hashlib.sha256(runner_bytes).hexdigest() != identity[2]
        or hashlib.sha256(state_bytes).hexdigest() != identity[3]
    ):
        raise PromotionError("current CONTROL runner or state source changed after authentication")
    identity_output = _control_git_output(
        pathlib.Path(identity[0]), ("rev-parse", "--show-toplevel", "HEAD"),
        deadline_monotonic=deadline_monotonic, max_bytes=4096,
    )
    try:
        identity_lines = identity_output.decode("utf-8").splitlines()
        if (
            len(identity_lines) != 2
            or pathlib.Path(identity_lines[0]).resolve(strict=True) != pathlib.Path(identity[0])
            or identity_lines[1] != identity[1]
            or os.environ.get("GITHUB_SHA") != identity[1]
        ):
            raise ValueError("current CONTROL workflow commit changed")
    except (UnicodeDecodeError, OSError, ValueError) as exc:
        raise PromotionError("current CONTROL workflow commit changed after authentication") from exc
    if known is None:
        runtime_baseline = _ack_module_runtime_seal(module)
        _CURRENT_CONTROL_STATE_MODULES[str(expected_path)] = (module, identity, runtime_baseline)
    elif known[2] != _ack_module_runtime_seal(module):
        raise PromotionError("current CONTROL Git state runtime changed after authentication")
    _check_absolute_deadline(deadline_monotonic, label="current CONTROL state validation")
    return module

def load_current_control_publication_state(
    control_root: pathlib.Path,
    *,
    deadline_monotonic: float,
) -> types.ModuleType:
    """Load or reuse only the byte-authenticated state module from this runner's CONTROL checkout."""
    _check_absolute_deadline(deadline_monotonic, label="current CONTROL state bootstrap")
    try:
        supplied_root = pathlib.Path(control_root).absolute()
        runner_file = pathlib.Path(__file__).absolute()
    except (OSError, TypeError, ValueError) as exc:
        raise PromotionError("current CONTROL execution root is unavailable") from exc
    if runner_file.name != "run-canonical-update-promotion.py" or runner_file.parent.name != "scripts":
        raise PromotionError("current CONTROL execution file is not the approved promotion runner")
    expected_control_root = runner_file.parent.parent
    if (
        supplied_root != expected_control_root
        or _path_has_symlink_component(runner_file)
        or _path_has_symlink_component(expected_control_root)
    ):
        raise PromotionError("current CONTROL root does not match the executing promotion runner")
    try:
        resolved_root = expected_control_root.resolve(strict=True)
    except OSError as exc:
        raise PromotionError("current CONTROL root cannot be resolved") from exc
    if not resolved_root.is_dir() or _path_has_symlink_component(resolved_root / "scripts"):
        raise PromotionError("current CONTROL root is not a safe repository directory")
    state_path = resolved_root / "scripts/canonical_publication_state.py"
    if _path_has_symlink_component(state_path):
        raise PromotionError("current CONTROL Git state source path contains a symlink")
    try:
        resolved_state_path = state_path.resolve(strict=True)
        runner_size = runner_file.stat().st_size
        state_size = resolved_state_path.stat().st_size
        if (
            runner_size < 1 or runner_size > MAX_CURRENT_CONTROL_SOURCE_BYTES
            or state_size < 1 or state_size > MAX_CURRENT_CONTROL_SOURCE_BYTES
        ):
            raise PromotionError("current CONTROL source files exceed their fixed size bound")
        current_runner_bytes = runner_file.read_bytes()
        current_state_bytes = resolved_state_path.read_bytes()
    except OSError as exc:
        raise PromotionError("current CONTROL source files are unavailable") from exc
    if (
        resolved_state_path != state_path
        or not current_runner_bytes or len(current_runner_bytes) > MAX_CURRENT_CONTROL_SOURCE_BYTES
        or not current_state_bytes or len(current_state_bytes) > MAX_CURRENT_CONTROL_SOURCE_BYTES
    ):
        raise PromotionError("current CONTROL source path or byte size is invalid")
    identity_output = _control_git_output(
        resolved_root, ("rev-parse", "--show-toplevel", "HEAD"),
        deadline_monotonic=deadline_monotonic, max_bytes=4096,
    )
    try:
        identity_lines = identity_output.decode("utf-8").splitlines()
        if len(identity_lines) != 2:
            raise ValueError("unexpected git identity row count")
        git_root = pathlib.Path(identity_lines[0]).resolve(strict=True)
        control_head_sha = identity_lines[1]
    except (UnicodeDecodeError, IndexError, OSError, ValueError) as exc:
        raise PromotionError("current CONTROL Git root or HEAD identity is malformed") from exc
    workflow_sha = os.environ.get("GITHUB_SHA")
    if (
        git_root != resolved_root
        or not isinstance(workflow_sha, str)
        or not re.fullmatch(r"[a-f0-9]{40}", workflow_sha)
        or workflow_sha != control_head_sha
    ):
        raise PromotionError("current CONTROL checkout does not match the executing workflow source")
    runner_git_bytes = _read_current_control_blob(
        resolved_root, control_head_sha, "scripts/run-canonical-update-promotion.py",
        deadline_monotonic=deadline_monotonic,
    )
    state_git_bytes = _read_current_control_blob(
        resolved_root, control_head_sha, "scripts/canonical_publication_state.py",
        deadline_monotonic=deadline_monotonic,
    )
    if current_runner_bytes != runner_git_bytes or current_state_bytes != state_git_bytes:
        raise PromotionError("current CONTROL working source differs from its authenticated workflow Git commit")
    identity = (
        str(resolved_root),
        control_head_sha,
        hashlib.sha256(runner_git_bytes).hexdigest(),
        hashlib.sha256(state_git_bytes).hexdigest(),
    )
    _check_absolute_deadline(deadline_monotonic, label="current CONTROL state bootstrap")
    existing = sys.modules.get("canonical_publication_state", _CONTROL_STATE_MISSING)
    if existing is not _CONTROL_STATE_MISSING:
        return _require_current_control_state_module(
            existing, state_path=state_path, identity=identity,
            deadline_monotonic=deadline_monotonic,
        )

    loader = _CurrentControlStateSourceLoader(
        state_git_bytes, filename=str(resolved_state_path), identity=identity,
    )
    spec = importlib.machinery.ModuleSpec("canonical_publication_state", loader, origin=str(resolved_state_path))
    spec.has_location = True
    spec.cached = None
    module = importlib.util.module_from_spec(spec)
    module.__file__ = str(resolved_state_path)
    module.__cached__ = None
    sys.modules["canonical_publication_state"] = module
    try:
        _check_absolute_deadline(deadline_monotonic, label="current CONTROL state bootstrap")
        loader.exec_module(module)
        module.__datapan_current_control_state_identity__ = identity
        return _require_current_control_state_module(
            module, state_path=state_path, identity=identity,
            deadline_monotonic=deadline_monotonic,
        )
    except BaseException:
        sys.modules.pop("canonical_publication_state", None)
        _CURRENT_CONTROL_STATE_MODULES.pop(str(resolved_state_path), None)
        raise

class _AuthenticatedLocalMReader:
    """Read one exact M through the already-authenticated current native state module."""

    def __init__(
        self,
        data_root: pathlib.Path,
        *,
        control_root: pathlib.Path,
        state_module: types.ModuleType,
        deadline_monotonic: float,
        member_path_count: int,
    ) -> None:
        _check_absolute_deadline(deadline_monotonic, label="release closure M reader")
        self.root = pathlib.Path(data_root).resolve(strict=True)
        self.control_root = pathlib.Path(control_root).resolve(strict=True)
        self.deadline_monotonic = deadline_monotonic
        self._store: Any | None = None
        if sys.modules.get("canonical_publication_state") is not state_module:
            raise PromotionError("authenticated current CONTROL Git state module singleton changed")
        state_path = self.control_root / "scripts/canonical_publication_state.py"
        module_path_value = getattr(state_module, "__file__", None)
        try:
            module_path = pathlib.Path(module_path_value).resolve(strict=True)
            expected_path = state_path.resolve(strict=True)
        except (OSError, TypeError, ValueError) as exc:
            raise PromotionError("authenticated current CONTROL Git state module path is unavailable") from exc
        if (
            state_path.is_symlink()
            or module_path != expected_path
            or self.control_root not in module_path.parents
        ):
            raise PromotionError("Git state module is not the exact current CONTROL singleton")
        store_type = getattr(state_module, "GitObjectStore", None)
        index_type = getattr(state_module, "PythonModuleIndex", None)
        entry_type = getattr(state_module, "PythonModuleIndexEntry", None)
        if (
            not isinstance(store_type, type)
            or store_type.__module__ != "canonical_publication_state"
            or not isinstance(index_type, type)
            or index_type.__module__ != "canonical_publication_state"
            or not isinstance(entry_type, type)
            or entry_type.__module__ != "canonical_publication_state"
            or getattr(sys.modules.get("canonical_publication_state"), "PythonModuleIndex", None) is not index_type
        ):
            raise PromotionError("Git state module does not expose its exact native index and store types")
        remaining = deadline_monotonic - time.monotonic()
        if remaining <= 0:
            raise PromotionError("release closure M reader deadline expired before local Git setup")
        if (
            isinstance(member_path_count, bool)
            or not isinstance(member_path_count, int)
            or not 1 <= member_path_count <= MAX_RELEASE_CLOSURE_MEMBER_PATHS
        ):
            raise PromotionError("release closure M member count is outside its fixed command budget")
        command_budget = (
            RELEASE_CLOSURE_GIT_COMMANDS_PER_BLOB * (member_path_count + 1)
            + RELEASE_CLOSURE_GIT_COMMAND_SETUP_RESERVE
        )
        try:
            # Use the checkout as a local Git source. This path performs no
            # remote discovery or network fetch and keeps all M reads in one
            # bounded native GitObjectStore budget.
            self._store = store_type(
                self.root,
                remote_url=str(self.root),
                timeout_seconds=remaining,
                max_commands=command_budget,
                max_output_bytes=192 * 1024 * 1024,
                max_blob_bytes=8 * 1024 * 1024,
                deadline_monotonic=deadline_monotonic,
            )
        except Exception as exc:  # noqa: BLE001 - do not expose Git/environment details
            raise PromotionError("authenticated local Git store could not be initialized") from exc
        self._state_module = state_module
        self._index_type = index_type

    def _check_deadline(self, deadline_monotonic: float) -> None:
        if (
            isinstance(deadline_monotonic, bool)
            or not isinstance(deadline_monotonic, (int, float))
            or deadline_monotonic != self.deadline_monotonic
            or time.monotonic() >= self.deadline_monotonic
        ):
            raise PromotionError("release closure M reads do not share the exact live ACK deadline")

    def read_blob(
        self, commit_sha: str, path: str, *, max_bytes: int,
        deadline_monotonic: float,
    ) -> bytes:
        self._check_deadline(deadline_monotonic)
        if (
            not isinstance(commit_sha, str) or not re.fullmatch(r"[a-f0-9]{40}", commit_sha)
            or isinstance(max_bytes, bool) or not isinstance(max_bytes, int)
            or max_bytes < 1 or max_bytes > 8 * 1024 * 1024
            or not isinstance(path, str) or not path
        ):
            raise PromotionError("release closure M blob request is outside its fixed bounds")
        assert self._store is not None
        try:
            with self._store.bounded_deadline(self.deadline_monotonic):
                raw = self._store.read_file(commit_sha, path, maximum=max_bytes)
        except Exception as exc:  # noqa: BLE001 - native errors may contain repository details
            raise PromotionError("release closure M Git blob read failed") from exc
        self._check_deadline(deadline_monotonic)
        if not isinstance(raw, bytes) or not raw or len(raw) > max_bytes:
            raise PromotionError("release closure M Git blob is absent or exceeds its fixed bound")
        return raw

    def read_python_module_index(
        self, commit_sha: str, *, deadline_monotonic: float,
    ) -> Any:
        self._check_deadline(deadline_monotonic)
        if not isinstance(commit_sha, str) or not re.fullmatch(r"[a-f0-9]{40}", commit_sha):
            raise PromotionError("release closure M module-index commit is malformed")
        assert self._store is not None
        try:
            with self._store.bounded_deadline(self.deadline_monotonic):
                index = self._store.read_python_module_index(
                    commit_sha,
                    deadline_monotonic=self.deadline_monotonic,
                )
        except Exception as exc:  # noqa: BLE001 - native errors may contain repository details
            raise PromotionError("release closure M module-index read failed") from exc
        self._check_deadline(deadline_monotonic)
        if type(index) is not self._index_type:
            raise PromotionError("release closure M module index differs from the current native singleton")
        return index

    def close(self) -> None:
        store, self._store = self._store, None
        if store is not None:
            try:
                store.close()
            except Exception as exc:  # noqa: BLE001 - cleanup failure blocks acknowledgment
                raise PromotionError("release closure M Git store cleanup failed") from exc

def verify_release_closure_publication_evidence(
    root: pathlib.Path,
    receipt: Mapping[str, Any],
    *,
    source_sha: str,
    deadline_monotonic: float | None = None,
    control_modules: _CurrentControlAckModules | None = None,
) -> None:
    """Verify purpose evidence from the exact merged candidate tree before ACK."""
    candidate = receipt.get("candidate")
    if not isinstance(candidate, Mapping):
        raise PromotionError("publication journal candidate is malformed")
    if not any(
        field in candidate
        for field in ("purpose", "release_closure_reference", "release_closure_evidence")
    ):
        return
    deadline = time.monotonic() + 120.0 if deadline_monotonic is None else deadline_monotonic
    _check_absolute_deadline(deadline, label="release closure ACK verification")
    control_root = pathlib.Path(__file__).absolute().parent.parent
    loaded_control = (
        load_current_control_ack_modules(control_root, deadline_monotonic=deadline)
        if control_modules is None
        else _require_current_control_ack_modules(
            control_modules, deadline_monotonic=deadline,
        )
    )
    _require_current_control_ack_modules(loaded_control, deadline_monotonic=deadline)
    helper = loaded_control.canonical_update_pr
    try:
        identity = helper.release_closure_identity(candidate)
    except Exception as exc:  # noqa: BLE001 - unknown or partial purpose must fail closed
        raise PromotionError("publication candidate has malformed release closure identity") from exc
    if identity is None:
        raise PromotionError("purpose-bearing candidate has no exact current CONTROL identity")
    reference, evidence_pointer = identity
    try:
        member_paths = loaded_control.closure.generator_consumer_membership_paths()
    except Exception as exc:  # noqa: BLE001 - the authenticated current CONTROL table owns this bound
        raise PromotionError("current CONTROL generator/consumer membership is unavailable") from exc
    if (
        not isinstance(member_paths, Sequence)
        or isinstance(member_paths, (str, bytes))
        or not 1 <= len(member_paths) <= MAX_RELEASE_CLOSURE_MEMBER_PATHS
        or any(not isinstance(path, str) or not path for path in member_paths)
    ):
        raise PromotionError("current CONTROL generator/consumer membership has an unsupported command budget")
    reader = _AuthenticatedLocalMReader(
        root,
        control_root=control_root,
        state_module=loaded_control.state,
        deadline_monotonic=deadline,
        member_path_count=len(member_paths),
    )
    try:
        _verify_release_closure_publication_evidence_with_reader(
            root,
            candidate,
            reference=reference,
            evidence_pointer=evidence_pointer,
            source_sha=source_sha,
            reader=reader,
            projection=loaded_control.projection,
            deadline_monotonic=deadline,
        )
        _require_current_control_ack_modules(loaded_control, deadline_monotonic=deadline)
    finally:
        reader.close()

def _verify_release_closure_publication_evidence_with_reader(
    root: pathlib.Path,
    candidate: Mapping[str, Any],
    *,
    reference: Mapping[str, Any],
    evidence_pointer: Mapping[str, Any],
    source_sha: str,
    reader: _AuthenticatedLocalMReader,
    projection: Any,
    deadline_monotonic: float,
) -> None:
    key = reference.get("refresh_key")
    expected_path = f"reports/data-go-kr/upstream-catalogue-review/release-closure/{key}/comparison.json"
    if evidence_pointer.get("path") != expected_path:
        raise PromotionError("release closure publication evidence path differs from the full-key reference")
    raw = reader.read_blob(
        source_sha,
        expected_path,
        max_bytes=MAX_NATIVE_RELEASE_CLOSURE_COMPARISON_BLOB_BYTES,
        deadline_monotonic=deadline_monotonic,
    )
    if (
        len(raw) != evidence_pointer.get("bytes")
        or hashlib.sha256(raw).hexdigest() != evidence_pointer.get("sha256")
    ):
        raise PromotionError("release closure publication evidence differs from its committed candidate identity")
    try:
        document = json.loads(
            raw.decode("utf-8"), object_pairs_hook=_strict_json_pairs,
            parse_constant=_reject_nonfinite_comparison_value,
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PromotionError("release closure comparison is not strict UTF-8 JSON") from exc
    if (
        not isinstance(document, Mapping)
        or document.get("purpose") != "release-closure-refresh"
        or document.get("release_closure_reference") != reference
        or not isinstance(document.get("original_generation"), Mapping)
        or not isinstance(document.get("source_eligibility_evidence"), Mapping)
    ):
        raise PromotionError("release closure comparison does not match its candidate purpose reference")
    try:
        parent_generation_id, parent_canonical_sha, parent_projection_sha, parent_ref_sha = (
            _validate_release_closure_comparison_parent(candidate, reference, document, projection)
        )
    except Exception as exc:  # noqa: BLE001 - both native reference versions are exact and closed
        if isinstance(exc, PromotionError):
            raise
        raise PromotionError("release closure comparison parent is not a supported exact native reference") from exc
    generation = document["original_generation"]
    comparison = document.get("comparison")
    modern_comparison = reference.get("schema_version") == "datapan.canonical-release-closure-refresh-reference.v2"
    expected_comparison_fields = (
        RELEASE_CLOSURE_COMPARISON_FIELDS if modern_comparison
        else RELEASE_CLOSURE_COMPARISON_LEGACY_FIELDS
    )
    if not isinstance(comparison, Mapping) or set(comparison) != expected_comparison_fields:
        raise PromotionError("release closure comparison has an incomplete or open projection record")
    if (
        candidate.get("generation_id") != generation.get("generation_id")
        or generation.get("generation_id") != parent_generation_id
        or candidate.get("registry_sha256") != parent_canonical_sha
        or reference.get("generation_id") != generation.get("generation_id")
        or reference.get("canonical_sha256") != parent_canonical_sha
        or comparison.get("canonical_sha256") != parent_canonical_sha
        or comparison.get("admitted_parent_ref_sha256") != parent_ref_sha
        or comparison.get("legacy_baseline_id") != reference.get("legacy_baseline_id")
        or comparison.get(
            "admitted_parent_projection_sha256" if modern_comparison else "legacy_parent_projection_sha256",
        ) != parent_projection_sha
        or comparison.get(
            "regenerated_projection_sha256" if modern_comparison else "generated_projection_sha256",
        ) != reference.get("generated_projection_sha256")
        or comparison.get("source_eligibility_evidence_sha256") != hashlib.sha256(
            canonical_json(dict(document["source_eligibility_evidence"]))
        ).hexdigest()
        or comparison.get("original_generation_sha256") != hashlib.sha256(
            canonical_json(dict(generation))
        ).hexdigest()
        or comparison.get("original_generation_epoch") != generation.get("epoch")
        or comparison.get("original_epoch_origin") != generation.get("epoch_origin")
        or comparison.get("original_observed_at") != projection.BASE._observation_from_generation(dict(generation))
        or comparison.get("current_base_delta_sha256") != reference.get("current_base_delta_sha256")
    ):
        raise PromotionError("release closure comparison changed its parent, generation, eligibility, or delta identity")
    try:
        if parent_ref_sha is not None:
            projection._validate_generation(dict(generation), document["admitted_parent_ref"])
        elif projection.BASE._observation_from_generation(dict(generation)) != comparison.get("original_observed_at"):
            raise ValueError("legacy original observation differs from its comparison")
    except Exception as exc:  # noqa: BLE001 - original observation/epoch provenance stays exact
        raise PromotionError("release closure comparison original generation is not explicitly normalized") from exc
    digest_fields = (
        (
            "ruleset_sha256", "canonical_sha256", "admitted_parent_ref_sha256",
            "admitted_parent_projection_sha256", "current_base_projection_sha256",
            "regenerated_projection_sha256", "current_base_delta_sha256",
            "source_eligibility_evidence_sha256", "original_generation_sha256",
        )
        if modern_comparison else (
            "ruleset_sha256", "canonical_sha256", "legacy_parent_projection_sha256",
            "current_base_projection_sha256", "generated_projection_sha256",
            "current_base_delta_sha256", "source_eligibility_evidence_sha256",
            "original_generation_sha256",
        )
    )
    for name in digest_fields:
        digest = comparison.get(name)
        if not isinstance(digest, str) or not re.fullmatch(r"[a-f0-9]{64}", digest):
            raise PromotionError(f"release closure comparison {name} is not a full SHA-256")
    if not isinstance(document.get("source_refresh_evidence_sha256"), str) or not re.fullmatch(
        r"[a-f0-9]{64}", document["source_refresh_evidence_sha256"],
    ):
        raise PromotionError("release closure comparison source-refresh evidence digest is malformed")
    for name in ("generator_consumer_closure_sha256",):
        digest = document.get(name)
        if not isinstance(digest, str) or not re.fullmatch(r"[a-f0-9]{64}", digest):
            raise PromotionError(f"release closure comparison {name} is not a full SHA-256")
    dependencies = document.get("provenance_dependency_digests")
    if not isinstance(dependencies, Mapping) or any(
        not isinstance(name, str) or not name
        or not isinstance(digest, str) or not re.fullmatch(r"[a-f0-9]{64}", digest)
        for name, digest in dependencies.items()
    ):
        raise PromotionError("release closure comparison provenance dependencies are malformed")
    try:
        base_identity = projection.BASE._validate_base_identity(comparison.get("current_preparation_base"))
        if base_identity["commit_sha"] != candidate.get("base_sha"):
            raise PromotionError("release closure M differs from the exact candidate base")
        actual_closure = projection.BASE.validate_generator_consumer_dependencies_at_m(
            dependencies,
            reader=reader,
            preparation_base=base_identity,
            deadline_monotonic=deadline_monotonic,
        )
    except PromotionError:
        raise
    except Exception as exc:  # noqa: BLE001 - exact M closure failure blocks publication acknowledgment
        raise PromotionError("release closure dependency map failed exact M authentication") from exc
    if actual_closure != document.get("generator_consumer_closure_sha256"):
        raise PromotionError("release closure digest differs from its authenticated M dependency closure")
    for name in ("parent_delta_paths", "current_base_delta_paths", "captured_changed_paths"):
        paths = comparison.get(name)
        if not isinstance(paths, list) or any(not isinstance(path, str) or not path for path in paths):
            raise PromotionError(f"release closure comparison {name} is malformed")
    if not comparison["parent_delta_paths"] or not comparison["current_base_delta_paths"]:
        raise PromotionError("release closure publication has no positive two-sided substantive comparison")
    for name, path_field in (("parent_delta_records", "parent_delta_paths"), ("current_base_delta_records", "current_base_delta_paths")):
        records = comparison.get(name)
        if not isinstance(records, list) or any(not isinstance(row, Mapping) for row in records):
            raise PromotionError(f"release closure comparison {name} is malformed")
        record_paths = [row.get("path") for row in records]
        if set(record_paths) != set(comparison[path_field]):
            raise PromotionError(f"release closure comparison {name} does not cover its exact path set")

def resolve_verified_source_target(
    root: pathlib.Path,
    state_root: pathlib.Path,
    repository: str,
    current_head_sha: str,
    *,
    evaluation: Mapping[str, Any],
) -> ResolvedSourceTarget | dict[str, Any]:
    """Resolve immutable original-A plus target identity before any new C parent.

    Non-C actions remain closed locator dictionaries. A C action returns a
    private capability only after the exact committed state, admitted original
    observation, classified Git target and a second state read all agree.
    """
    root = root.resolve()
    state_root = state_root.resolve()
    eligibility = load_module(
        root / "scripts/upstream_catalogue_source_eligibility.py",
        "trusted_c_source_target_eligibility",
    )
    evaluated = eligibility.validate_evaluation(evaluation)
    if evaluated["source_sha"] != current_head_sha:
        raise PromotionError("source target evaluation is not pinned to current main")
    actual_tree = command(("git", "rev-parse", f"{current_head_sha}^{{tree}}"), root).stdout.strip()
    if evaluated["source_tree_sha"] != actual_tree:
        raise PromotionError("source target evaluation tree differs from current main")
    selection_state_sha = _source_eligibility_selection_state_revision(state_root, repository)
    request = eligibility.source_work_request_from_state(
        root=root,
        state_dir=state_root,
        repository=repository,
        source_id="data_go_kr",
        source_scope="aggregate_supported_catalog",
        evaluation=evaluated,
        selection_state_sha=selection_state_sha,
    )
    if request.get("selection_state_sha") != selection_state_sha:
        raise PromotionError("source target request does not bind the preselection state revision")
    if request.get("action") != "revalidate_c":
        if _source_eligibility_selection_state_revision(state_root, repository) != selection_state_sha:
            raise PromotionError("source target selection state changed during locator resolution")
        return request
    if selection_state_sha is None:
        raise PromotionError("source target selection has no authenticated processor-state revision")
    generation_id = str(request.get("selected_generation_id") or "")
    schema_path = root / "schemas/datapan.upstream-catalogue-checkpoint.v1.schema.json"
    first = _source_eligibility_state_snapshot(
        state_root, repository, generation_id, schema_path,
        eligibility_module=eligibility,
    )
    if first["state_commit"] != selection_state_sha:
        raise PromotionError("source target selection state changed before immutable target snapshot")
    checkpoint = first["checkpoint"]
    handoff = load_module(root / "scripts/upstream_catalogue_handoff.py", "trusted_c_target_handoff")
    derivation = load_module(root / "scripts/upstream_catalogue_derivation.py", "trusted_c_target_derivation")
    preparer = load_module(
        root / "scripts/prepare-upstream-catalogue-derivation.py",
        "trusted_c_target_preparer",
    )
    admission = preparer.admission_for_checkpoint(checkpoint, first["index"], handoff)
    original = derivation.original_observation_from_checkpoint(checkpoint, admission)
    target_contract, target_inventory = eligibility.target_contract_from_predecessor(
        root, current_head_sha, checkpoint, original,
    )
    if target_contract != request.get("target_contract"):
        raise PromotionError("source target classification differs from its bounded locator")
    if target_inventory != eligibility.validate_inventory(request.get("target_inventory")):
        raise PromotionError("source target inventory differs from its bounded locator")
    selection_key = eligibility.selection_key(admission, target_contract)
    original_generation = eligibility.normalize_original_generation(checkpoint)

    second = _source_eligibility_state_snapshot(
        state_root, repository, generation_id, schema_path,
        eligibility_module=eligibility,
    )
    if any(
        first[field] != second[field]
        for field in ("state_commit", "index_bytes", "checkpoint_bytes")
    ):
        raise PromotionError("source target state changed during immutable resolution")
    second_admission = preparer.admission_for_checkpoint(
        second["checkpoint"], second["index"], handoff,
    )
    second_original = derivation.original_observation_from_checkpoint(
        second["checkpoint"], second_admission,
    )
    if second_admission != admission or second_original != original:
        raise PromotionError("source target admitted observation changed during immutable resolution")
    state_refs = [
        {
            "role": "admission_index", "repository": repository, "kind": "git",
            "commit_sha": first["state_commit"], "path": first["index_relative"],
            "bytes": len(first["index_bytes"]),
            "sha256": hashlib.sha256(first["index_bytes"]).hexdigest(),
        },
        {
            "role": "processor_checkpoint", "repository": repository, "kind": "git",
            "commit_sha": first["state_commit"], "path": first["checkpoint_relative"],
            "bytes": len(first["checkpoint_bytes"]),
            "sha256": hashlib.sha256(first["checkpoint_bytes"]).hexdigest(),
        },
    ]
    return ResolvedSourceTarget(
        _RESOLVED_SOURCE_TARGET,
        selection_key=selection_key,
        reason_code=str(request.get("reason_code") or ""),
        evaluation=evaluated,
        original_admission=admission,
        original_observation=original,
        original_generation=original_generation,
        target_contract=target_contract,
        target_inventory=target_inventory,
        generation_id=generation_id,
        state_refs=state_refs,
        state_snapshot=first,
    )

if __name__ == "__main__":
    raise SystemExit(main())
