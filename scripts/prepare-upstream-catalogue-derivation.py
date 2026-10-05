#!/usr/bin/env python3
"""Prepare a fully authenticated same-observation B derivation before claim.

The command is an intake coordinator only. It reads the durable B index, exact
Actions attempts/artifacts, the current main/LFS identity, and one frozen C
journal row. It writes local temporary evidence for the normal processor; it
never changes a checkpoint, consumes detail budget, creates a PR, or publishes.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import importlib.util
import json
import pathlib
import re
import shutil
import sys
from collections.abc import Mapping
from typing import Any


def load_module(path: pathlib.Path, name: str) -> Any:
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise ValueError("derivation_validator_unavailable")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def load_json(path: pathlib.Path, maximum_bytes: int = 16 * 1024 * 1024) -> Any:
    if path.is_symlink() or not path.is_file() or path.stat().st_size > maximum_bytes:
        raise ValueError("derivation_input_file_invalid")
    return json.loads(path.read_bytes())


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def run_git(root: pathlib.Path, *argv: str) -> str:
    import subprocess

    result = subprocess.run(("git", *argv), cwd=root, text=True, capture_output=True, check=False)
    if result.returncode != 0:
        raise ValueError("derivation_git_identity_unavailable")
    return result.stdout.strip()


def admission_for_checkpoint(
    checkpoint: Mapping[str, Any], index: Mapping[str, Any], handoff: Any,
) -> dict[str, Any]:
    ledger = index.get("collector_handoff")
    if not isinstance(ledger, Mapping):
        raise ValueError("derivation_admission_ledger_missing")
    validated = handoff.validate_ledger(ledger)
    observation = checkpoint.get("last_observation")
    refs = checkpoint.get("input_artifacts")
    if not isinstance(observation, Mapping) or not isinstance(refs, list):
        raise ValueError("derivation_parent_admission_identity_missing")
    matches = [
        row for row in validated["admitted_observations"]
        if row.get("producer_run_id") == str(observation.get("producer_run_id") or "")
        and row.get("refresh_evidence_sha256") == observation.get("refresh_evidence_sha256")
        and any(
            isinstance(ref, Mapping)
            and str(ref.get("run_id")) == row["producer_run_id"]
            and str(ref.get("artifact_id")) == row["artifact_id"]
            and ref.get("evidence_sha256") == row["refresh_evidence_sha256"]
            for ref in refs
        )
    ]
    if len(matches) != 1:
        raise ValueError("derivation_parent_admission_ambiguous_or_missing")
    return dict(matches[0])


def authenticate_parent_bundle(
    root: pathlib.Path, repository: str, default_branch: str, state_root: pathlib.Path,
    checkpoint: Mapping[str, Any], bundle_dir: pathlib.Path, runner: Any,
) -> dict[str, Any]:
    """Bind one terminal checkpoint to a trusted exact B attempt and archive."""
    run_id, attempt, _name = runner.processor_attempt_from_locator(checkpoint)
    run = runner.processor_run_api(root, repository, run_id, attempt)
    runner.validate_trusted_processor_run(
        run, repository=repository, run_id=run_id, attempt=attempt,
        default_branch=default_branch,
    )
    locator = checkpoint.get("output_artifact")
    if not isinstance(locator, Mapping):
        raise ValueError("derivation_parent_artifact_locator_missing")
    artifact = runner.processor_artifact_api(root, repository, run_id, str(locator.get("artifact_id") or ""))
    if artifact is None:
        raise ValueError("derivation_parent_artifact_missing")
    artifact = runner.validate_processor_artifact_metadata(
        artifact, checkpoint, run, repository=repository,
    )
    composition_schema = runner.load_object(root / "schemas/datapan.catalogue-composition-receipt.v1.schema.json")
    composition_helper = runner.load_canonical_update_pr(root)
    validated = runner.validate_processor_bundle(
        checkpoint, bundle_dir, composition_schema, composition_helper, root=root,
    )
    if validated.get("status") not in {"ready", "no-change"}:
        raise ValueError("derivation_parent_is_not_reviewable")
    return dict(validated)


def download_parent_bundle(
    root: pathlib.Path, repository: str, default_branch: str, state_root: pathlib.Path,
    checkpoint: Mapping[str, Any], destination: pathlib.Path, runner: Any,
) -> pathlib.Path:
    run_id, _attempt, _name = runner.processor_attempt_from_locator(checkpoint)
    run = runner.processor_run_api(root, repository, run_id, _attempt)
    runner.validate_trusted_processor_run(
        run, repository=repository, run_id=run_id, attempt=_attempt,
        default_branch=default_branch,
    )
    locator = checkpoint.get("output_artifact")
    if not isinstance(locator, Mapping):
        raise ValueError("derivation_parent_artifact_locator_missing")
    artifact = runner.processor_artifact_api(root, repository, run_id, str(locator.get("artifact_id") or ""))
    if artifact is None:
        raise ValueError("derivation_parent_artifact_missing")
    artifact = runner.validate_processor_artifact_metadata(artifact, checkpoint, run, repository=repository)
    runner.download_processor_artifact(root, repository, artifact, destination)
    return destination


def local_bundle_matches_checkpoint(checkpoint: Mapping[str, Any], bundle_dir: pathlib.Path) -> bool:
    """Treat the workflow cache as an optimization, never as a parent selector."""
    outputs = checkpoint.get("output_digests")
    if not isinstance(outputs, list) or not outputs:
        return False
    for entry in outputs:
        if not isinstance(entry, Mapping):
            return False
        relative = pathlib.PurePosixPath(str(entry.get("path") or ""))
        if relative.is_absolute() or ".." in relative.parts or "\\" in str(entry.get("path") or ""):
            return False
        path = bundle_dir.joinpath(*relative.parts)
        if (
            path.is_symlink() or not path.is_file()
            or path.stat().st_size != entry.get("bytes")
            or sha256_bytes(path.read_bytes()) != entry.get("sha256")
        ):
            return False
    return True


def exact_ack_run(
    root: pathlib.Path, repository: str, row: Mapping[str, Any],
    health_policy: Mapping[str, Any], now: dt.datetime, derivation: Any,
) -> None:
    """Require the exact successful C workflow attempt that recorded the merge."""
    derivation.authenticate_canonical_merge_ack(root, repository, row, health_policy, now=now)


def build_plan(args: argparse.Namespace) -> dict[str, Any]:
    main_root = args.main_root.resolve()
    source_root = args.source_root.resolve()
    state_root = args.state_dir.resolve()
    output_root = args.output_dir.resolve()
    repository = args.repository
    if not re.fullmatch(r"[^/]+/[^/]+", repository):
        raise ValueError("derivation_repository_invalid")
    if not args.target_generation_id:
        return {"eligible": False, "reason": "new_observation_no_existing_processor_parent"}

    runner = load_module(main_root / "scripts/run-canonical-update-promotion.py", "derivation_promotion_runner")
    handoff = load_module(main_root / "scripts/upstream_catalogue_handoff.py", "derivation_handoff")
    health = load_module(main_root / "scripts/check-upstream-catalogue-health.py", "derivation_health_trust")
    derivation = load_module(main_root / "scripts/upstream_catalogue_derivation.py", "derivation_schema")
    checkpoint_schema = main_root / "schemas/datapan.upstream-catalogue-checkpoint.v1.schema.json"
    source_state = state_root / "sources/data_go_kr"
    index = load_json(source_state / "index.json", 2 * 1024 * 1024)
    if index.get("schema_version") != "datapan.upstream-catalogue-checkpoint.v1" or not isinstance(index.get("generations"), list):
        raise ValueError("derivation_state_index_invalid")
    indexed_ids = {
        row.get("generation_id") for row in index["generations"] if isinstance(row, Mapping)
    }
    target_id = args.target_generation_id
    if target_id not in indexed_ids:
        return {"eligible": False, "reason": "selected_generation_not_durable"}
    target_path = source_state / "generations" / f"{target_id}.json"
    target = runner.verify_processor_checkpoint(load_json(target_path, 262144), checkpoint_schema)
    if target.get("source_id") != "data_go_kr" or not isinstance(target.get("source_scope"), str) or not target.get("source_scope"):
        raise ValueError("derivation_selected_source_mismatch")

    candidate_path = args.candidate.resolve()
    evidence_path = args.refresh_evidence.resolve()
    diff_path = args.diff.resolve()
    admission = handoff.validate_admission_row(load_json(args.collector_admission, 2 * 1024 * 1024))
    evidence = load_json(evidence_path, 256 * 1024 * 1024)
    if not isinstance(evidence, Mapping) or evidence.get("collection", {}).get("succeeded") is not True:
        return {"eligible": False, "reason": "current_observation_not_successful"}
    producer_head = str(admission.get("head_sha") or "")
    if run_git(source_root, "rev-parse", "HEAD") != producer_head:
        raise ValueError("derivation_source_checkout_does_not_match_producer_head")
    policy_bytes = (source_root / "policy/source-refresh.json").read_bytes()
    adapter_bytes = (source_root / "data/provider-index.json").read_bytes()
    for relative, working_bytes in (
        ("policy/source-refresh.json", policy_bytes),
        ("data/provider-index.json", adapter_bytes),
    ):
        if run_git(main_root, "show", f"{producer_head}:{relative}").encode("utf-8") != working_bytes:
            raise ValueError("derivation_original_A_policy_or_provider_identity_unbound")
    original = derivation.validate_original_observation({
        "source_id": "data_go_kr",
        "source_scope": target["source_scope"],
        "producer_run_id": str(admission["producer_run_id"]),
        "producer_run_attempt": admission["run_attempt"],
        "producer_head_sha": admission["head_sha"],
        "producer_artifact_id": str(admission["artifact_id"]),
        "producer_artifact_name": admission["artifact_name"],
        "producer_artifact_sha256": admission["artifact_digest_sha256"],
        "observed_at": admission["observed_at"],
        "observation_count": target.get("observation_count"),
        "original_baseline_sha256": sha256_bytes(source_root.joinpath("data/data-go-kr.registry.json").read_bytes()),
        "candidate_sha256": sha256_bytes(candidate_path.read_bytes()),
        "evidence_sha256": sha256_bytes(evidence_path.read_bytes()),
        "diff_sha256": sha256_bytes(diff_path.read_bytes()),
        "source_policy_sha256": sha256_bytes(policy_bytes),
        "provider_index_sha256": sha256_bytes(adapter_bytes),
    })
    if admission.get("candidate_sha256") != original["candidate_sha256"] or admission.get("refresh_evidence_sha256") != original["evidence_sha256"]:
        raise ValueError("derivation_current_admission_artifact_mismatch")
    target_admission = admission_for_checkpoint(target, index, handoff)
    target_original = derivation.original_observation_from_checkpoint(target, target_admission)
    if target_original != original:
        return {"eligible": False, "reason": "selected_parent_is_different_source_observation"}
    if target.get("status") not in {"ready", "no-change"}:
        return {"eligible": False, "reason": "selected_parent_not_reviewable"}
    selected_target = target
    prior_outcome = target.get("outcome") if isinstance(target.get("outcome"), Mapping) else {}
    has_work = int(prior_outcome.get("detail_retry_count", 0) or 0) > 0

    journal, journal_ref_sha = runner.load_promotion_journal_snapshot(main_root)
    if not isinstance(journal, Mapping) or not isinstance(journal_ref_sha, str):
        return {"eligible": False, "reason": "canonical_journal_unavailable"}
    current_head = run_git(main_root, "rev-parse", "HEAD")
    current_identity = runner.authenticated_current_canonical_registry(main_root, current_head)
    active_rows = [
        row for row in journal.get("records", []) if isinstance(row, Mapping)
        and row.get("superseded_by") is None
        and row.get("status") in {"merged", "publication-pending", "published", "read-back-confirmed"}
        and isinstance(row.get("candidate"), Mapping)
        and row["candidate"].get("repository", "").casefold() == repository.casefold()
        and row["candidate"].get("source_id") == original["source_id"]
        and row["candidate"].get("scope") == original["source_scope"]
        and row["candidate"].get("registry_path") == current_identity["registry_path"]
        and row["candidate"].get("registry_bytes") == current_identity["registry_bytes"]
        and row["candidate"].get("registry_sha256") == current_identity["registry_sha256"]
    ]
    same_observation_rows: list[tuple[int, dict[str, Any], dict[str, Any], dict[str, Any]]] = []
    for row in active_rows:
        generation_id = str(row["candidate"].get("generation_id") or "")
        if generation_id not in indexed_ids:
            continue
        cp = runner.verify_processor_checkpoint(
            load_json(source_state / "generations" / f"{generation_id}.json", 262144), checkpoint_schema,
        )
        cp_admission = admission_for_checkpoint(cp, index, handoff)
        if derivation.original_observation_from_checkpoint(cp, cp_admission) != original:
            continue
        index_rows = journal.get("records", [])
        position = next((number for number, candidate in enumerate(index_rows) if candidate is row), -1)
        if position < 0:
            raise ValueError("canonical_journal_row_locator_unavailable")
        readback = derivation.canonical_parent_readback_reference(
            journal, journal_ref_sha=journal_ref_sha, record_index=position,
        )
        same_observation_rows.append((position, dict(row), cp, readback))
    if len(same_observation_rows) != 1:
        if len(same_observation_rows) > 1:
            raise ValueError("same_observation_canonical_lineage_ambiguous")
        return {"eligible": False, "reason": "no_same_observation_merged_canonical_lineage"}

    canonical_index, canonical_row, canonical_checkpoint, readback = same_observation_rows[0]
    if canonical_checkpoint["generation_id"] == selected_target["generation_id"]:
        # Once a derived B is itself the current C producer, reuse that B as
        # the canonical parent and retain its authenticated resume parent.
        # Durable retry counters still come from the current index; this old
        # parent supplies only A-bound cache/provenance bytes.
        selected_inputs = selected_target.get("generation_inputs")
        prior_envelope = selected_inputs.get("same_observation_derivation") if isinstance(selected_inputs, Mapping) else None
        if prior_envelope is not None:
            prior = derivation.validate_derivation_envelope(prior_envelope)
            if prior["original_observation"] != original:
                raise ValueError("canonical_producer_parent_crosses_original_observation")
            resume_generation_id = prior["resume_parent_processor"]["generation_id"]
        else:
            resume_generation_id = ""
            for indexed in reversed(index.get("generations", [])):
                generation_id = indexed.get("generation_id") if isinstance(indexed, Mapping) else None
                if not isinstance(generation_id, str) or generation_id == selected_target["generation_id"]:
                    continue
                path = source_state / "generations" / f"{generation_id}.json"
                if not path.is_file() or path.is_symlink():
                    continue
                candidate_parent = runner.verify_processor_checkpoint(load_json(path, 262144), checkpoint_schema)
                if candidate_parent.get("status") not in {"ready", "no-change"}:
                    continue
                candidate_admission = admission_for_checkpoint(candidate_parent, index, handoff)
                if derivation.original_observation_from_checkpoint(candidate_parent, candidate_admission) == original:
                    resume_generation_id = generation_id
                    break
            if not resume_generation_id:
                # This B checkpoint can independently satisfy both the
                # contribution-resume and C-canonical roles. The role proofs
                # remain separate in the envelope; distinct object identity
                # is not required when no older same-A B is retained.
                resume_generation_id = str(selected_target["generation_id"])
        if resume_generation_id not in indexed_ids:
            raise ValueError("canonical_producer_resume_parent_not_indexed")
        resume_path = source_state / "generations" / f"{resume_generation_id}.json"
        if not resume_path.is_file() or resume_path.is_symlink():
            raise ValueError("canonical_producer_resume_parent_unavailable")
        target = runner.verify_processor_checkpoint(load_json(resume_path, 262144), checkpoint_schema)
        target_admission = admission_for_checkpoint(target, index, handoff)
        if (
            target.get("status") not in {"ready", "no-change"}
            or derivation.original_observation_from_checkpoint(target, target_admission) != original
        ):
            raise ValueError("canonical_producer_resume_parent_invalid")
    if not has_work and target.get("output_artifact", {}).get("bundle_manifest_sha256") == canonical_checkpoint.get("output_artifact", {}).get("bundle_manifest_sha256"):
        return {"eligible": False, "reason": "no_incremental_processor_work"}

    now = dt.datetime.now(dt.timezone.utc)
    target_expiry = runner.parse_utc_timestamp(target["output_artifact"].get("expires_at"), "resume parent artifact expiry")
    canonical_expiry = runner.parse_utc_timestamp(canonical_checkpoint["output_artifact"].get("expires_at"), "canonical parent artifact expiry")
    if min(target_expiry, canonical_expiry) <= now:
        raise ValueError("derivation_parent_artifact_expired")

    # Reuse the workflow's downloaded archive when it is the selected parent;
    # otherwise fetch the exact retained artifact named by the stored parent.
    if target["generation_id"] == selected_target["generation_id"] and local_bundle_matches_checkpoint(
        target, args.resume_bundle.resolve(),
    ):
        target_bundle_dir = args.resume_bundle.resolve()
    else:
        target_bundle_dir = output_root / "resume-parent-bundle"
        download_parent_bundle(
            main_root, repository, args.default_branch, state_root, target,
            target_bundle_dir, runner,
        )
    target_bundle = authenticate_parent_bundle(
        main_root, repository, args.default_branch, state_root, target,
        target_bundle_dir, runner,
    )
    target_evidence = load_json(target_bundle_dir / "upstream-catalogue-enrichment-evidence.json", 256 * 1024 * 1024)
    target_reference = derivation.processor_parent_reference(
        target, repository=repository, original_observation=original,
        enrichment_evidence=target_evidence,
        enrichment_evidence_bytes=(target_bundle_dir / "upstream-catalogue-enrichment-evidence.json").read_bytes(),
    )

    if canonical_checkpoint["generation_id"] == target["generation_id"]:
        canonical_dir = target_bundle_dir
    elif canonical_checkpoint["generation_id"] == selected_target["generation_id"] and local_bundle_matches_checkpoint(
        canonical_checkpoint, args.resume_bundle.resolve(),
    ):
        canonical_dir = args.resume_bundle.resolve()
    else:
        canonical_dir = output_root / "canonical-parent-bundle"
        download_parent_bundle(
            main_root, repository, args.default_branch, state_root, canonical_checkpoint,
            canonical_dir, runner,
        )
    canonical_bundle = authenticate_parent_bundle(
        main_root, repository, args.default_branch, state_root, canonical_checkpoint,
        canonical_dir, runner,
    )
    canonical_evidence = load_json(canonical_dir / "upstream-catalogue-enrichment-evidence.json", 256 * 1024 * 1024)
    canonical_reference = derivation.processor_parent_reference(
        canonical_checkpoint, repository=repository, original_observation=original,
        enrichment_evidence=canonical_evidence,
        enrichment_evidence_bytes=(canonical_dir / "upstream-catalogue-enrichment-evidence.json").read_bytes(),
    )
    if canonical_bundle.get("registry_sha256") != readback["registry_sha256"]:
        raise ValueError("canonical_parent_bundle_differs_from_merged_C_candidate")

    readback_row = runner.gh_pr_readback(main_root, repository, int(readback["pr_number"]))
    merge_commit = readback_row.get("mergeCommit")
    observed_merge_sha = merge_commit.get("oid") if isinstance(merge_commit, Mapping) else None
    if (
        readback_row.get("number") != readback["pr_number"]
        or readback_row.get("state") != "CLOSED"
        or readback_row.get("repository") != repository
        or readback_row.get("headRepository") != repository
        or readback_row.get("headRefName") != readback["pr_branch"]
        or readback_row.get("headRefOid") != readback["pr_head_sha"]
        or readback_row.get("baseRefName") != args.default_branch
        or observed_merge_sha != readback["merge_sha"]
        or readback_row.get("merged") is not True
        or not isinstance(readback_row.get("body"), str)
        or sha256_bytes(readback_row["body"].encode("utf-8")) != readback["pr_body_sha256"]
        or readback_row["body"] != canonical_row.get("ownership", {}).get("body")
    ):
        raise ValueError("canonical_parent_PR_live_readback_mismatch")
    exact_ack_run(
        main_root, repository, canonical_row,
        load_json(main_root / "policy/upstream-catalogue-health.json"), now, derivation,
    )

    composition_baseline_path = main_root / ".datapan/current-canonical" / pathlib.Path(*current_identity["registry_path"].split("/"))
    baseline_bytes = composition_baseline_path.read_bytes()
    baseline = {
        "main_sha": current_identity["main_sha"],
        "manifest_sha256": current_identity["manifest_sha256"],
        "registry_path": current_identity["registry_path"],
        "registry_sha256": current_identity["registry_sha256"],
        "registry_bytes": current_identity["registry_bytes"],
    }
    if len(baseline_bytes) != baseline["registry_bytes"] or sha256_bytes(baseline_bytes) != baseline["registry_sha256"]:
        raise ValueError("derivation_current_canonical_materialization_mismatch")

    def load_lineage_checkpoint(generation_id: str) -> dict[str, Any]:
        if generation_id not in indexed_ids:
            raise ValueError("derivation_parent_graph_checkpoint_unavailable")
        path = source_state / "generations" / f"{generation_id}.json"
        if not path.is_file() or path.is_symlink():
            raise ValueError("derivation_parent_graph_checkpoint_unavailable")
        return runner.verify_processor_checkpoint(load_json(path, 262144), checkpoint_schema)

    def lineage_admission(checkpoint: Mapping[str, Any]) -> dict[str, Any]:
        return admission_for_checkpoint(checkpoint, index, handoff)

    resume_graph = derivation.validate_processor_parent_graph(
        [str(target["generation_id"])], load_checkpoint=load_lineage_checkpoint,
        admission_for=lineage_admission, original_observation=original,
    )
    canonical_graph = derivation.validate_processor_parent_graph(
        [str(canonical_checkpoint["generation_id"])], load_checkpoint=load_lineage_checkpoint,
        admission_for=lineage_admission, original_observation=original,
    )
    processor = load_module(main_root / "scripts/process-upstream-catalogue-candidate.py", "derivation_processor_identity")
    envelope = derivation.build_derivation_envelope(
        original_observation=original,
        resume_parent_processor=target_reference,
        canonical_parent_processor=canonical_reference,
        canonical_parent_readback=readback,
        composition_baseline=baseline,
        derivation_processor_revision_sha256=processor.derivation_processor_revision(),
        resume_parent_ancestors=sorted(resume_graph - {str(target["generation_id"])}),
        canonical_parent_ancestors=sorted(canonical_graph - {str(canonical_checkpoint["generation_id"])}),
    )
    output_root.mkdir(parents=True, exist_ok=True)
    target_bundle_dir_string = str(target_bundle_dir)
    if not pathlib.Path(target_bundle_dir_string).is_dir():
        raise ValueError("derivation_resume_parent_bundle_unavailable")
    composition_output = output_root / "composition-baseline.registry.json"
    composition_output.write_bytes(baseline_bytes)
    envelope_path = output_root / "same-observation-derivation.json"
    envelope_path.write_bytes(derivation.canonical_json(envelope) + b"\n")
    (output_root / "canonical-update-promotion-journal.json").write_bytes(
        runner.canonical_json(journal) + b"\n",
    )
    generation_id, _generation_inputs = processor.generation_identity(
        original["source_id"], original["source_scope"], original["original_baseline_sha256"],
        original["candidate_sha256"], None, original["source_policy_sha256"],
        original["provider_index_sha256"], same_observation_derivation=envelope,
    )
    return {
        "eligible": True,
        "reason": "same_observation_canonical_derivation_authenticated",
        "generation_id": generation_id,
        "composition_baseline_path": str(composition_output),
        "derivation_path": str(envelope_path),
        "journal_path": str(output_root / "canonical-update-promotion-journal.json"),
        "journal_ref_sha": journal_ref_sha,
        "resume_parent_bundle_dir": str(target_bundle_dir),
        "resume_enrichment_evidence_path": str(target_bundle_dir / "upstream-catalogue-enrichment-evidence.json"),
        "canonical_parent_bundle_dir": str(canonical_dir),
        "current_main_sha": current_identity["main_sha"],
        "current_manifest_sha256": current_identity["manifest_sha256"],
        "resume_parent_generation_id": target["generation_id"],
        "canonical_parent_generation_id": canonical_checkpoint["generation_id"],
        "canonical_parent_journal_record_index": canonical_index,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--main-root", type=pathlib.Path, required=True)
    parser.add_argument("--source-root", type=pathlib.Path, required=True)
    parser.add_argument("--state-dir", type=pathlib.Path, required=True)
    parser.add_argument("--target-generation-id", default="")
    parser.add_argument("--repository", required=True)
    parser.add_argument("--default-branch", default="main")
    parser.add_argument("--collector-admission", type=pathlib.Path, required=True)
    parser.add_argument("--candidate", type=pathlib.Path, required=True)
    parser.add_argument("--refresh-evidence", type=pathlib.Path, required=True)
    parser.add_argument("--diff", type=pathlib.Path, required=True)
    parser.add_argument("--resume-bundle", type=pathlib.Path, required=True)
    parser.add_argument("--output-dir", type=pathlib.Path, required=True)
    parser.add_argument("--github-output", type=pathlib.Path)
    args = parser.parse_args()
    try:
        result = build_plan(args)
        args.output_dir.mkdir(parents=True, exist_ok=True)
        result_path = args.output_dir / "preparation-result.json"
        result_path.write_bytes(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True).encode("utf-8") + b"\n")
        if args.github_output:
            with args.github_output.open("a", encoding="utf-8") as handle:
                handle.write(f"derivation_enabled={str(result.get('eligible') is True).lower()}\n")
                for field in (
                    "composition_baseline_path", "derivation_path", "journal_path",
                    "journal_ref_sha", "resume_parent_bundle_dir", "resume_enrichment_evidence_path",
                    "canonical_parent_bundle_dir",
                ):
                    handle.write(f"{field}={result.get(field, '')}\n")
        print(json.dumps({key: value for key, value in result.items() if key != "envelope"}, sort_keys=True))
        return 0
    except Exception as exc:
        print(json.dumps({"eligible": False, "reason": str(exc)}, sort_keys=True), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
