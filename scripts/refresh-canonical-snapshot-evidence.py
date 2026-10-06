#!/usr/bin/env python3
"""Regenerate source-dependent release evidence before the release-ledger fixed point."""

from __future__ import annotations

import argparse
import hashlib
import json
import pathlib
import shlex
import subprocess
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass


@dataclass(frozen=True)
class SourceCommand:
    argv: tuple[str, ...]
    cwd: pathlib.Path

    @property
    def label(self) -> str:
        return shlex.join(self.argv)


class SourceRefreshError(RuntimeError):
    pass


def pinned_cli_revision(repository_root: pathlib.Path) -> str:
    policy_path = repository_root / "policy/external-checkout-refs.json"
    policy = json.loads(policy_path.read_text(encoding="utf-8"))
    revision = policy.get("ref")
    if policy.get("repository", "").casefold() != "statpan/datapan-cli" or not isinstance(revision, str):
        raise SourceRefreshError("external CLI checkout policy is malformed")
    if len(revision) != 40 or any(character not in "0123456789abcdef" for character in revision):
        raise SourceRefreshError("external CLI checkout policy does not contain a full immutable SHA")
    return revision


def read_registry(path: pathlib.Path) -> tuple[int, str]:
    try:
        data = path.read_bytes()
    except OSError as exc:
        raise SourceRefreshError(f"candidate registry cannot be read: {path}") from exc
    if data.startswith(b"version https://git-lfs.github.com/spec/v1"):
        raise SourceRefreshError("candidate registry is an unmaterialized Git LFS pointer")
    try:
        parsed = json.loads(data)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SourceRefreshError("candidate registry is not valid JSON") from exc
    if not isinstance(parsed, list) or any(not isinstance(row, dict) for row in parsed):
        raise SourceRefreshError("candidate registry must be an array of objects")
    return len(data), hashlib.sha256(data).hexdigest()


def repository_relative_input(path: pathlib.Path, repository_root: pathlib.Path, expected_path: str) -> str:
    """Return the canonical repository-relative label for a checked input."""
    root = repository_root.resolve()
    resolved = path.resolve()
    try:
        relative = resolved.relative_to(root)
    except ValueError as exc:
        raise SourceRefreshError("repository-local report input resolves outside the repository root") from exc
    if relative.as_posix() != expected_path:
        raise SourceRefreshError("repository-local report input does not resolve to its canonical path")
    if not resolved.is_file():
        raise SourceRefreshError("repository-local report input is not a regular file")
    return relative.as_posix()


def build_commands(
    repository_root: pathlib.Path,
    datapan_cli: pathlib.Path,
    registry: pathlib.Path,
    verification: pathlib.Path,
    previous_registry: pathlib.Path | None = None,
) -> tuple[SourceCommand, ...]:
    root = repository_root.resolve()
    cli = datapan_cli.resolve()
    registry_abs = registry.resolve()
    verification_abs = verification.resolve()
    runtime_plan_registry = repository_relative_input(
        registry_abs, root, "data/data-go-kr.registry.json",
    )
    runtime_plan_verification = repository_relative_input(
        verification_abs, root, "reports/latest-verification.json",
    )
    generated = root / "reports"
    native = ("go", "run", "./cmd/datapan")
    commands = [
        SourceCommand(native + ("catalog", "audit", "--registry", str(registry_abs), "--output", str(generated / "catalog-audit.json"), "--json"), cli),
        SourceCommand(native + ("catalog", "errors", "--registry", str(registry_abs), "--limit", "0", "--output", str(generated / "error-catalog.json"), "--json"), cli),
        SourceCommand(native + ("catalog", "dependencies", "--registry", str(registry_abs), "--limit", "0", "--output", str(generated / "dependencies.json"), "--json"), cli),
        SourceCommand(native + ("catalog", "adapter-targets", "--registry", str(registry_abs), "--limit", "0", "--output", str(generated / "adapter-targets.json"), "--json"), cli),
        SourceCommand(native + ("catalog", "providers", "--registry", str(registry_abs), "--limit", "0", "--output", str(generated / "provider-backlog.json"), "--json"), cli),
    ]
    if previous_registry is not None:
        previous_abs = previous_registry.resolve()
        commands.append(SourceCommand(
            native + ("catalog", "diff", "--old", str(previous_abs), "--new", str(registry_abs), "--limit", "0", "--output", str(generated / "catalog-diff.json"), "--json"),
            cli,
        ))
    route = [*native, "catalog", "route-disposition", "--registry", str(registry_abs), "--limit", "0", "--output", str(generated / "route-disposition.json"), "--json"]
    probe_path = generated / "unadapted-external-probe.json"
    if probe_path.is_file():
        route[route.index("--limit"):route.index("--limit")] = ["--probe", str(probe_path)]
    commands.append(SourceCommand(tuple(route), cli))
    commands.extend((
        SourceCommand(native + ("catalog", "coverage", "--registry", str(registry_abs), "--verification", str(verification_abs), "--route-disposition", str(generated / "route-disposition.json"), "--limit", "0", "--output", str(generated / "coverage.json"), "--json"), cli),
        SourceCommand(native + ("catalog", "verify", "plan", "--registry", str(registry_abs), "--verification", str(verification_abs), "--limit", "0", "--output", str(generated / "verification-plan.json"), "--json"), cli),
    ))

    def py(script: str, *args: str) -> SourceCommand:
        return SourceCommand(("python3", f"scripts/{script}", *args), root)

    completeness_args = [
        "--write",
        "--candidate-registry", str(registry_abs),
        "--candidate-operation-manifest", str(root / "reports/data-go-kr/operation-manifest.json"),
    ]
    if previous_registry is not None:
        completeness_args.extend(("--candidate-baseline-registry", str(previous_registry.resolve())))
    completeness_rollup = py("generate-completeness-proof-rollup.py", *completeness_args)

    return tuple(commands) + (
        # Project native CLI coverage into the existing source-denominator
        # contract before any consumer reads it.
        py("generate-data-go-kr-operation-denominator.py"),
        py("generate-data-go-kr-operation-manifest.py", "--registry", str(registry_abs)),
        py("update-data-go-kr-operation-denominator-expectation.py", "--write", "--registry", str(registry_abs)),
        py("sync-release-manifest-artifacts.py", "--write"),
        py("validate-data-go-kr-operation-manifest.py"),
        py("generate-operation-denominator-rollup.py"),
        # Issue #660 projects retained immutable runtime rows only after the
        # exact candidate operation contract exists.
        py(
            "generate-current-runtime-evidence-projection.py",
            "--registry", str(registry_abs),
            "--operation-manifest", str(root / "reports/data-go-kr/operation-manifest.json"),
            "--latest-verification", str(verification_abs),
            "--policy", str(root / "policy/sustainable-coverage.json"),
            "--output", str(root / "reports/current-runtime-evidence-projection.json"),
        ),
        py("generate-runtime-freshness-queue.py"),
        py("generate-health-probe-catalog.py"),
        # Health catalog validation reads the release manifest, so bind the
        # refreshed report before its downstream validator runs.
        py("sync-release-manifest-artifacts.py", "--write"),
        py("validate-health-probe-catalog.py"),
        # Preserve the historical diagnostic contract while explicitly
        # evaluating its current-source applicability against the exact
        # candidate and regenerated health catalog. The report is pending
        # evidence, never current approval or publication authority.
        py("generate-diagnostic-current-source-applicability.py", "--write"),
        py("sync-release-manifest-artifacts.py", "--write"),
        py("generate-diagnostic-current-source-applicability.py", "--check"),
        py("validate-diagnostic-current-source-applicability.py"),
        py("generate-runtime-evidence-growth.py"),
        py("validate-runtime-evidence-growth.py"),
        py("generate-operation-assertion-policies.py"),
        py("validate-operation-assertion-policies.py"),
        py("generate-diagnostic-release-candidate.py"),
        py("validate-diagnostic-envelope-draft.py"),
        py("validate-diagnostic-evidence-mapping-draft.py"),
        py("validate-diagnostic-release-candidate.py"),
        py("generate-diagnostic-publication.py", "--write"),
        py("generate-diagnostic-publication.py", "--check"),
        py(
            "generate-coverage-backlog.py",
            "--registry", str(registry_abs),
            "--latest-verification", str(verification_abs),
        ),
        py("validate-coverage-backlog.py"),
        py("generate-external-adapter-backlog.py"),
        py("validate-external-adapter-backlog.py"),
        py("generate-operation-materialization-plan.py"),
        py("validate-operation-materialization-plan.py"),
        py(
            "generate-institution-api-overview.py",
            "--registry", str(registry_abs),
            "--latest-verification", str(verification_abs),
        ),
        py("validate-institution-api-overview.py"),
        py(
            "generate-institution-runtime-plan.py",
            "--registry", runtime_plan_registry,
            "--latest-verification", runtime_plan_verification,
        ),
        py("validate-institution-runtime-plan.py"),
        py("generate-sustainable-coverage.py"),
        py("generate-readme-runtime-snapshot.py"),
        completeness_rollup,
        py("sync-release-schema-artifacts.py", "--write"),
        py("sync-release-manifest-artifacts.py", "--write"),
    )


def validate_root_source_scripts(commands: Sequence[SourceCommand], repository_root: pathlib.Path) -> None:
    root = repository_root.resolve()
    for source_command in commands:
        if source_command.cwd != root or len(source_command.argv) < 2:
            continue
        if source_command.argv[0] != "python3" or not source_command.argv[1].startswith("scripts/"):
            continue
        script = root / source_command.argv[1]
        if not script.is_file() or script.is_symlink():
            raise SourceRefreshError(f"required source refresh command is missing or unsafe: {source_command.argv[1]}")


def checked_out_revision(datapan_cli: pathlib.Path, run: Callable[[Sequence[str], pathlib.Path], subprocess.CompletedProcess[str]]) -> str:
    result = run(("git", "rev-parse", "HEAD"), datapan_cli)
    if result.returncode != 0:
        raise SourceRefreshError("datapan-cli checkout revision could not be read")
    revision = result.stdout.strip()
    if len(revision) != 40 or any(character not in "0123456789abcdef" for character in revision):
        raise SourceRefreshError("datapan-cli HEAD is not a full immutable Git commit SHA")
    return revision


def assert_clean_pinned_cli_tree(
    datapan_cli: pathlib.Path,
    expected_revision: str,
    expected_tree: str,
    run: Callable[[Sequence[str], pathlib.Path], subprocess.CompletedProcess[str]],
) -> None:
    revision = checked_out_revision(datapan_cli, run)
    if revision != expected_revision:
        raise SourceRefreshError("datapan-cli revision changed during source generation")
    status = run(("git", "status", "--porcelain", "--untracked-files=all"), datapan_cli)
    if status.returncode != 0 or status.stdout.strip():
        raise SourceRefreshError("datapan-cli source checkout became dirty during source generation")
    tree = run(("git", "rev-parse", "HEAD^{tree}"), datapan_cli)
    if tree.returncode != 0 or tree.stdout.strip() != expected_tree:
        raise SourceRefreshError("datapan-cli source tree changed during source generation")


def run_source_refresh(
    *,
    repository_root: pathlib.Path,
    datapan_cli: pathlib.Path,
    registry: pathlib.Path,
    verification: pathlib.Path,
    previous_registry: pathlib.Path | None = None,
    stable_generated_at: str | None = None,
    run: Callable[[Sequence[str], pathlib.Path], subprocess.CompletedProcess[str]] | None = None,
) -> tuple[str, tuple[str, ...], dict[str, object]]:
    runner = run or (lambda argv, cwd: subprocess.run(argv, cwd=cwd, text=True, capture_output=True, check=False))
    root, cli = repository_root.resolve(), datapan_cli.resolve()
    if not cli.is_dir() or not (cli / ".git").exists():
        raise SourceRefreshError("an already-checked-out datapan-cli Git repository is required; no checkout is created")
    expected_revision = pinned_cli_revision(root)
    actual_revision = checked_out_revision(cli, runner)
    if actual_revision != expected_revision:
        raise SourceRefreshError(f"datapan-cli checkout is not the reviewed immutable revision {expected_revision}")
    status = runner(("git", "status", "--porcelain", "--untracked-files=all"), cli)
    if status.returncode != 0 or status.stdout.strip():
        raise SourceRefreshError("datapan-cli source checkout is dirty; an immutable reviewed tree is required")
    tree_result = runner(("git", "rev-parse", "HEAD^{tree}"), cli)
    if tree_result.returncode != 0 or not tree_result.stdout.strip():
        raise SourceRefreshError("datapan-cli source tree identity could not be bound")
    actual_tree = tree_result.stdout.strip()
    if len(actual_tree) != 40 or any(character not in "0123456789abcdef" for character in actual_tree):
        raise SourceRefreshError("datapan-cli source tree is not a full immutable Git tree SHA")
    registry_abs, verification_abs = registry.resolve(), verification.resolve()
    if registry_abs != (root / "data/data-go-kr.registry.json").resolve():
        raise SourceRefreshError("the admitted candidate must occupy the canonical data.go.kr registry path in its isolated worktree")
    if verification_abs != (root / "reports/latest-verification.json").resolve():
        raise SourceRefreshError("source refresh must use the existing candidate-bound latest verification report")
    if not registry_abs.is_file() or not verification_abs.is_file():
        raise SourceRefreshError("admitted candidate registry and existing verification evidence are required")
    if registry_abs.is_symlink() or verification_abs.is_symlink():
        raise SourceRefreshError("candidate registry and verification evidence must be regular files, not symlinks")
    try:
        verification_report = json.loads(verification_abs.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SourceRefreshError("existing verification evidence is unreadable or invalid JSON") from exc
    if not isinstance(verification_report, dict):
        raise SourceRefreshError("existing verification evidence must be an object")
    registry_bytes, registry_sha256 = read_registry(registry_abs)
    verification_bytes = verification_abs.read_bytes()
    verification_sha256 = hashlib.sha256(verification_bytes).hexdigest()
    previous_abs = previous_registry.resolve() if previous_registry is not None else None
    previous_bytes: bytes | None = None
    previous_sha256: str | None = None
    if previous_abs is not None:
        if not previous_abs.is_file() or previous_abs.is_symlink() or not previous_abs.is_relative_to(root):
            raise SourceRefreshError("previous registry must be a regular in-repository baseline file")
        _previous_registry_bytes, previous_sha256 = read_registry(previous_abs)
        previous_bytes = previous_abs.read_bytes()
    stable_generated_at = stable_generated_at or "2000-01-01T00:00:00Z"
    try:
        import datetime as dt
        parsed_time = dt.datetime.fromisoformat(stable_generated_at.replace("Z", "+00:00"))
    except ValueError as exc:
        raise SourceRefreshError("source generation time must be an ISO-8601 timestamp") from exc
    if parsed_time.tzinfo is None:
        raise SourceRefreshError("source generation time must include a timezone")
    expected_registry_bytes = registry_abs.read_bytes()
    expected_registry_sha = hashlib.sha256(expected_registry_bytes).hexdigest()
    labels: list[str] = []
    stable_timestamp = parsed_time.astimezone(dt.timezone.utc).isoformat().replace("+00:00", "Z")
    commands = build_commands(root, cli, registry_abs, verification_abs, previous_registry)
    if run is None:
        validate_root_source_scripts(commands, root)
    native_receipts: list[dict[str, object]] = []
    native_output_paths: list[pathlib.Path] = []
    inventory_synced = False
    for source_command in commands:
        if source_command.cwd == root and not inventory_synced:
            for argv in (
                ("python3", "scripts/sync-release-schema-artifacts.py", "--write"),
                ("python3", "scripts/sync-release-manifest-artifacts.py", "--write"),
            ):
                print("+ " + shlex.join(argv), flush=True)
                synchronized = runner(argv, root)
                labels.append(shlex.join(argv))
                if synchronized.returncode != 0:
                    raise SourceRefreshError("release inventory synchronization failed after pinned native generators")
                if read_registry(registry_abs) != (len(expected_registry_bytes), expected_registry_sha):
                    raise SourceRefreshError("release inventory synchronization changed the exact candidate registry")
            inventory_synced = True

        print(f"+ [{source_command.cwd}] {source_command.label}", flush=True)
        if read_registry(registry_abs) != (len(expected_registry_bytes), expected_registry_sha):
            raise SourceRefreshError("candidate registry changed before a source-dependent generator")
        if verification_abs.is_symlink() or verification_abs.read_bytes() != verification_bytes:
            raise SourceRefreshError("latest-verification input changed during source refresh")
        if previous_abs is not None and (previous_abs.is_symlink() or previous_abs.read_bytes() != previous_bytes):
            raise SourceRefreshError("previous registry baseline changed during source refresh")
        if source_command.cwd == cli:
            assert_clean_pinned_cli_tree(cli, expected_revision, actual_tree, runner)
            argv = source_command.argv
            registry_arg = registry_abs.as_posix()
            if "--registry" in argv:
                input_index = argv.index("--registry") + 1
            elif "--new" in argv:
                input_index = argv.index("--new") + 1
            else:
                raise SourceRefreshError("pinned native command has no explicit candidate registry input")
            if argv[input_index] != registry_arg:
                raise SourceRefreshError("pinned native command does not use the exact admitted candidate registry path")
        result = runner(source_command.argv, source_command.cwd)
        labels.append(source_command.label)
        if result.returncode != 0:
            raise SourceRefreshError(f"source refresh failed before ledger refresh: {source_command.label} (exit {result.returncode})")
        if read_registry(registry_abs) != (len(expected_registry_bytes), expected_registry_sha):
            raise SourceRefreshError("a source-dependent generator changed the exact admitted candidate registry")
        if verification_abs.is_symlink() or verification_abs.read_bytes() != verification_bytes:
            raise SourceRefreshError("latest-verification input changed during source refresh")
        if previous_abs is not None and (previous_abs.is_symlink() or previous_abs.read_bytes() != previous_bytes):
            raise SourceRefreshError("previous registry baseline changed during source refresh")
        if source_command.cwd == cli:
            assert_clean_pinned_cli_tree(cli, expected_revision, actual_tree, runner)
            if "--output" not in source_command.argv:
                raise SourceRefreshError("pinned native generator does not write an explicit digest-bound output")
            output_path = pathlib.Path(source_command.argv[source_command.argv.index("--output") + 1]).resolve()
            if not output_path.is_relative_to(root) or not output_path.is_file():
                raise SourceRefreshError("pinned native generator output is missing or outside the repository")
            if output_path.is_symlink():
                raise SourceRefreshError("pinned native generator output must not be a symlink")
            raw_output = output_path.read_bytes()
            raw_output_bytes = len(raw_output)
            raw_output_sha256 = hashlib.sha256(raw_output).hexdigest()
            try:
                generated = json.loads(raw_output)
            except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise SourceRefreshError(f"pinned native generator emitted invalid JSON: {output_path}") from exc
            if isinstance(generated, dict) and isinstance(generated.get("generated_at"), str):
                generated["generated_at"] = stable_timestamp
                output_path.write_text(json.dumps(generated, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            native_output_paths.append(output_path)
            native_receipts.append({
                "argv": list(source_command.argv), "exit_code": 0,
                "input_registry_sha256": expected_registry_sha,
                **({"input_verification_sha256": verification_sha256} if "--verification" in source_command.argv else {}),
                **({"input_previous_registry_sha256": previous_sha256} if "--old" in source_command.argv else {}),
                "output_path": output_path.relative_to(root).as_posix(),
                "raw_output_bytes": raw_output_bytes,
                "raw_output_sha256": raw_output_sha256,
                "output_bytes": output_path.stat().st_size,
                "output_sha256": hashlib.sha256(output_path.read_bytes()).hexdigest(),
            })
    if not inventory_synced:
        raise SourceRefreshError("native source refresh had no checked-in source report generators")
    if registry_abs.read_bytes() != expected_registry_bytes or read_registry(registry_abs) != (len(expected_registry_bytes), expected_registry_sha):
        raise SourceRefreshError("candidate registry bytes changed during native source refresh")
    if verification_abs.is_symlink() or verification_abs.read_bytes() != verification_bytes:
        raise SourceRefreshError("latest-verification input changed during source refresh")
    if previous_abs is not None and (previous_abs.is_symlink() or previous_abs.read_bytes() != previous_bytes):
        raise SourceRefreshError("previous registry baseline changed during source refresh")
    applicability_path = root / "reports/diagnostic-current-source-applicability.json"
    applicability_schema_path = root / "schemas/datapan.diagnostic-current-source-applicability.v1.schema.json"
    if (
        not applicability_path.is_file()
        or applicability_path.is_symlink()
        or not applicability_schema_path.is_file()
        or applicability_schema_path.is_symlink()
    ):
        raise SourceRefreshError("current-source diagnostic applicability report and schema are required")
    applicability_bytes = applicability_path.read_bytes()
    applicability_schema_bytes = applicability_schema_path.read_bytes()
    try:
        applicability = json.loads(applicability_bytes)
        applicability_schema = json.loads(applicability_schema_bytes)
    except Exception as exc:  # noqa: BLE001 - the independent validator must fail closed
        raise SourceRefreshError("current-source diagnostic applicability report is malformed") from exc
    if not isinstance(applicability, dict):
        raise SourceRefreshError("current-source diagnostic applicability report must be an object")
    authority = applicability.get("authority")
    if not isinstance(authority, dict) or any(value is not False for value in authority.values()):
        raise SourceRefreshError("current-source diagnostic applicability must not grant approval or publication authority")
    try:
        import jsonschema

        jsonschema.Draft202012Validator(
            applicability_schema, format_checker=jsonschema.FormatChecker()
        ).validate(applicability)
    except Exception as exc:  # noqa: BLE001 - the independent validator must fail closed
        raise SourceRefreshError("current-source diagnostic applicability report is malformed") from exc
    if applicability.get("schema_version") != "datapan.diagnostic-current-source-applicability.v1":
        raise SourceRefreshError("current-source diagnostic applicability report has an unsupported schema")
    if applicability.get("status") not in {"historical_scope_unchanged", "revalidation_required"}:
        raise SourceRefreshError("current-source diagnostic applicability status is unsupported")
    current_inputs = applicability.get("current_inputs")
    current_registry = current_inputs.get("registry") if isinstance(current_inputs, dict) else None
    current_health = current_inputs.get("health_catalog") if isinstance(current_inputs, dict) else None
    health_path = root / "reports/health-probe-catalog.json"
    if not health_path.is_file() or health_path.is_symlink():
        raise SourceRefreshError("regenerated health probe catalog is missing or unsafe")
    health_bytes = health_path.read_bytes()
    if current_registry != {
        "path": registry_abs.relative_to(root).as_posix(),
        "bytes": len(expected_registry_bytes),
        "sha256": expected_registry_sha,
    }:
        raise SourceRefreshError("diagnostic applicability does not bind the exact admitted candidate registry")
    if current_health != {
        "path": "reports/health-probe-catalog.json",
        "bytes": len(health_bytes),
        "sha256": hashlib.sha256(health_bytes).hexdigest(),
    }:
        raise SourceRefreshError("diagnostic applicability does not bind the exact regenerated health catalog")
    applicability_evidence = {
        "path": applicability_path.relative_to(root).as_posix(),
        "bytes": len(applicability_bytes),
        "sha256": hashlib.sha256(applicability_bytes).hexdigest(),
        "schema_path": applicability_schema_path.relative_to(root).as_posix(),
        "schema_bytes": len(applicability_schema_bytes),
        "schema_sha256": hashlib.sha256(applicability_schema_bytes).hexdigest(),
        "status": applicability["status"],
    }
    source_evidence: dict[str, object] = {
        "schema_version": "datapan.canonical-source-refresh-evidence.v1",
        "registry_path": registry_abs.relative_to(root).as_posix(),
        "registry_bytes": registry_bytes,
        "registry_sha256": registry_sha256,
        "verification_path": verification_abs.relative_to(root).as_posix(),
        "verification_bytes": len(verification_bytes),
        "verification_sha256": verification_sha256,
        "datapan_cli_revision": actual_revision,
        "datapan_cli_tree": actual_tree,
        "generated_at": stable_timestamp,
        "output_normalization": "replace_top_level_generated_at_with_source_observation_time_utc_then_json_indent_2_newline",
        "diagnostic_current_source_applicability": applicability_evidence,
        "commands": native_receipts,
    }
    print(json.dumps({
        "status": "source-refreshed",
        "datapan_cli_revision": actual_revision,
        "registry_bytes": registry_bytes,
        "registry_sha256": registry_sha256,
        "commands_completed": len(labels),
    }, sort_keys=True))
    return registry_sha256, tuple(labels), source_evidence


def run_ledger_refresh(
    repository_root: pathlib.Path,
    run: Callable[[Sequence[str], pathlib.Path], subprocess.CompletedProcess[str]] | None = None,
) -> None:
    runner = run or (lambda argv, cwd: subprocess.run(argv, cwd=cwd, text=True, capture_output=True, check=False))
    command = ("python3", "scripts/refresh-release-ledger-evidence.py", "--write")
    print("+ " + shlex.join(command), flush=True)
    result = runner(command, repository_root.resolve())
    if result.returncode != 0:
        raise SourceRefreshError("release-ledger fixed-point refresh failed after source refresh")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repository-root", type=pathlib.Path, default=pathlib.Path("."))
    parser.add_argument("--datapan-cli", type=pathlib.Path, required=True, help="existing checkout at the reviewed CLI commit; this command never fetches or checks out")
    parser.add_argument("--registry", type=pathlib.Path, default=pathlib.Path("data/data-go-kr.registry.json"))
    parser.add_argument("--verification", type=pathlib.Path, default=pathlib.Path("reports/latest-verification.json"))
    parser.add_argument("--previous-registry", type=pathlib.Path)
    parser.add_argument("--with-ledger", action="store_true", help="run the existing bounded ledger fixed-point refresh after all source generators pass")
    args = parser.parse_args()
    try:
        run_source_refresh(
            repository_root=args.repository_root,
            datapan_cli=args.datapan_cli,
            registry=args.registry if args.registry.is_absolute() else args.repository_root / args.registry,
            verification=args.verification if args.verification.is_absolute() else args.repository_root / args.verification,
            previous_registry=(args.previous_registry if args.previous_registry is None or args.previous_registry.is_absolute() else args.repository_root / args.previous_registry),
        )
        if args.with_ledger:
            run_ledger_refresh(args.repository_root)
    except Exception as exc:  # noqa: BLE001
        print(f"FAIL canonical snapshot evidence refresh: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
