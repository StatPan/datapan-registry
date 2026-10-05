from __future__ import annotations

import copy
import contextlib
import datetime as dt
import hashlib
import importlib.util
import json
import os
import pathlib
import shutil
import subprocess
import tempfile
import unittest
from typing import Any
from unittest import mock


ROOT = pathlib.Path(__file__).parents[1]


def load_script(name: str, path: pathlib.Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


FLOW_TESTS = load_script(
    "same_observation_c_delivery_flow_helpers",
    ROOT / "tests/test_same_observation_derivation.py",
)
PROMOTION = FLOW_TESTS.PROMOTION
DERIVATION = FLOW_TESTS.DERIVATION
PROCESSOR = FLOW_TESTS.PROCESSOR
HANDOFF = FLOW_TESTS.HANDOFF
MATERIALIZER = load_script(
    "same_observation_c_delivery_materializer",
    ROOT / "scripts/materialize-canonical-registry.py",
)


def read_json(path: pathlib.Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def registry_pointer(payload: bytes) -> bytes:
    return (
        "version https://git-lfs.github.com/spec/v1\n"
        f"oid sha256:{hashlib.sha256(payload).hexdigest()}\n"
        f"size {len(payload)}\n"
    ).encode("ascii")


class SourceRefreshTestAdapter:
    """Deterministic adapter for the external native report generators only."""

    OUTPUTS = {
        ("catalog", "audit"): "reports/catalog-audit.json",
        ("catalog", "errors"): "reports/error-catalog.json",
        ("catalog", "dependencies"): "reports/dependencies.json",
        ("catalog", "adapter-targets"): "reports/adapter-targets.json",
        ("catalog", "providers"): "reports/provider-backlog.json",
        ("catalog", "diff"): "reports/catalog-diff.json",
        ("catalog", "route-disposition"): "reports/route-disposition.json",
        ("catalog", "coverage"): "reports/coverage.json",
        ("catalog", "verify", "plan"): "reports/verification-plan.json",
    }

    def run_source_refresh(
        self,
        *,
        repository_root: pathlib.Path,
        datapan_cli: pathlib.Path,
        registry: pathlib.Path,
        verification: pathlib.Path,
        previous_registry: pathlib.Path | None = None,
        stable_generated_at: str | None = None,
    ) -> tuple[str, tuple[str, ...], dict[str, Any]]:
        del datapan_cli
        root = repository_root
        payload = registry.read_bytes()
        registry_sha = hashlib.sha256(payload).hexdigest()
        verification_bytes = verification.read_bytes()
        previous_sha = hashlib.sha256(previous_registry.read_bytes()).hexdigest() if previous_registry else None

        applicability_path = root / "reports/diagnostic-current-source-applicability.json"
        applicability = read_json(applicability_path)
        applicability["current_inputs"]["registry"] = {
            "path": "data/data-go-kr.registry.json",
            "bytes": len(payload),
            "sha256": registry_sha,
        }
        applicability_bytes = json.dumps(
            applicability, ensure_ascii=False, indent=2,
        ).encode("utf-8") + b"\n"
        applicability_path.write_bytes(applicability_bytes)

        schema_path = root / "schemas/datapan.diagnostic-current-source-applicability.v1.schema.json"
        schema_bytes = schema_path.read_bytes()
        health_path = root / "reports/health-probe-catalog.json"
        health_bytes = health_path.read_bytes()
        verification_sha = hashlib.sha256(verification_bytes).hexdigest()

        commands: list[dict[str, Any]] = []
        for key, output_path in self.OUTPUTS.items():
            output = root / output_path
            output.parent.mkdir(parents=True, exist_ok=True)
            output_bytes = json.dumps(
                {"test_adapter": "external-native-report-generator", "report": output_path},
                sort_keys=True,
            ).encode("utf-8") + b"\n"
            output.write_bytes(output_bytes)
            if key == ("catalog", "diff"):
                argv = [
                    "go", "run", "./cmd/datapan", "catalog", "diff",
                    "--old", str(previous_registry), "--new", str(registry),
                    "--limit", "0", "--output", str(output), "--json",
                ]
            elif key == ("catalog", "route-disposition"):
                argv = [
                    "go", "run", "./cmd/datapan", "catalog", "route-disposition",
                    "--registry", str(registry), "--probe",
                    str(root / "reports/unadapted-external-probe.json"),
                    "--limit", "0", "--output", str(output), "--json",
                ]
            elif key == ("catalog", "coverage"):
                argv = [
                    "go", "run", "./cmd/datapan", "catalog", "coverage",
                    "--registry", str(registry), "--verification", str(verification),
                    "--route-disposition", str(root / "reports/route-disposition.json"),
                    "--limit", "0", "--output", str(output), "--json",
                ]
            elif key == ("catalog", "verify", "plan"):
                argv = [
                    "go", "run", "./cmd/datapan", "catalog", "verify", "plan",
                    "--registry", str(registry), "--verification", str(verification),
                    "--output", str(output), "--json",
                ]
            else:
                command_name = key[1]
                argv = [
                    "go", "run", "./cmd/datapan", "catalog", command_name,
                    "--registry", str(registry), "--limit", "0", "--output", str(output), "--json",
                ]
            record: dict[str, Any] = {
                "argv": argv,
                "exit_code": 0,
                "input_registry_sha256": registry_sha,
                "output_path": output_path,
                "raw_output_bytes": len(output_bytes),
                "raw_output_sha256": hashlib.sha256(output_bytes).hexdigest(),
                "output_bytes": len(output_bytes),
                "output_sha256": hashlib.sha256(output_bytes).hexdigest(),
            }
            if "--verification" in argv:
                record["input_verification_sha256"] = verification_sha
            if key == ("catalog", "diff"):
                record["input_previous_registry_sha256"] = previous_sha
            commands.append(record)

        pinned = read_json(root / "policy/external-checkout-refs.json")["ref"]
        applicability_report = json.loads(applicability_bytes)
        evidence = {
            "schema_version": "datapan.canonical-source-refresh-evidence.v1",
            "registry_path": "data/data-go-kr.registry.json",
            "registry_bytes": len(payload),
            "registry_sha256": registry_sha,
            "verification_path": "reports/latest-verification.json",
            "verification_bytes": len(verification_bytes),
            "verification_sha256": verification_sha,
            "datapan_cli_revision": pinned,
            "datapan_cli_tree": "1" * 40,
            "generated_at": stable_generated_at,
            "output_normalization": "replace_top_level_generated_at_with_source_observation_time_utc_then_json_indent_2_newline",
            "diagnostic_current_source_applicability": {
                "path": "reports/diagnostic-current-source-applicability.json",
                "bytes": len(applicability_bytes),
                "sha256": hashlib.sha256(applicability_bytes).hexdigest(),
                "schema_path": "schemas/datapan.diagnostic-current-source-applicability.v1.schema.json",
                "schema_bytes": len(schema_bytes),
                "schema_sha256": hashlib.sha256(schema_bytes).hexdigest(),
                "status": applicability_report["status"],
            },
            "commands": commands,
        }
        return registry_sha, ("test external source refresh",), evidence

    @staticmethod
    def run_ledger_refresh(_root: pathlib.Path) -> None:
        # The production ledger refresh is covered elsewhere; this test holds
        # its deterministic generated inputs fixed while testing C delivery.
        return None


class FakeGitHubLfsMaterializer:
    """Keep PR/LFS transport local while running canonical_update_pr validators."""

    def __init__(self, root: pathlib.Path, promotion_repo: "PromotionRepo") -> None:
        self.root = root
        self.promotion_repo = promotion_repo
        self.remote_branches: dict[str, str] = {}

    def git_output(self, arguments: list[str], cwd: pathlib.Path, *, availability: bool = False) -> bytes:
        del availability
        if arguments[:3] == ["ls-remote", "--heads", "origin"]:
            ref = arguments[3]
            sha = self.promotion_repo.current_main if ref == "refs/heads/main" else self.remote_branches.get(ref)
            return f"{sha}\t{ref}\n".encode("ascii") if sha else b""
        if arguments[:2] == ["remote", "get-url"]:
            return b"https://github.com/StatPan/datapan-registry.git\n"
        if arguments and arguments[0] == "push":
            refspec = arguments[-1]
            source, _, ref = refspec.partition(":")
            self.remote_branches[ref] = source
            self.promotion_repo.push_count += 1
            return b""
        return subprocess.run(
            ["git", *arguments], cwd=cwd, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, check=True,
        ).stdout

    def run_git(self, arguments: list[str], cwd: pathlib.Path) -> subprocess.CompletedProcess[bytes]:
        return subprocess.run(
            ["git", *arguments], cwd=cwd, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, check=False,
        )

    load_object = staticmethod(MATERIALIZER.load_object)
    registry_identity = staticmethod(MATERIALIZER.registry_identity)
    manifest_sha256 = staticmethod(MATERIALIZER.manifest_sha256)
    preparation_backend = staticmethod(MATERIALIZER.preparation_backend)
    repository_from_remote = staticmethod(MATERIALIZER.repository_from_remote)
    parse_lfs_pointer = staticmethod(MATERIALIZER.parse_lfs_pointer)
    lfs_object_path = staticmethod(MATERIALIZER.lfs_object_path)
    validate = staticmethod(MATERIALIZER.validate)

    def tracked_policy_binding(self, policy_path: pathlib.Path, repository_root: pathlib.Path, commit: str) -> dict[str, Any]:
        relative = policy_path.resolve().relative_to(repository_root.resolve()).as_posix()
        working = policy_path.read_bytes()
        committed = self.git_output(["show", f"{commit}:{relative}"], repository_root)
        if working != committed:
            raise AssertionError("test materializer observed changed candidate policy bytes")
        return {
            "policy_path": relative,
            "policy_bytes": len(committed),
            "policy_sha256": hashlib.sha256(committed).hexdigest(),
        }

    def lfs_storage_from_env(self, repository_root: pathlib.Path) -> pathlib.Path:
        output = self.git_output(["lfs", "env"], repository_root).decode("utf-8")
        return pathlib.Path(next(line.partition("=")[2] for line in output.splitlines() if line.startswith("LfsStorageDir=")))

    def github_lfs_materialize(
        self, policy: dict[str, Any], policy_path: pathlib.Path, manifest_path: pathlib.Path,
        registry_path: pathlib.Path, expected_bytes: int, expected_sha256: str,
        destination: pathlib.Path, *, candidate_commit: str,
        expected_manifest_sha256: str, remote: str, check_only: bool,
    ) -> dict[str, Any]:
        del policy, registry_path, remote, check_only
        if self.git_output(["rev-parse", "HEAD"], manifest_path.parent).decode().strip() != candidate_commit:
            raise AssertionError("candidate checkout changed before fake LFS readback")
        if hashlib.sha256(manifest_path.read_bytes()).hexdigest() != expected_manifest_sha256:
            raise AssertionError("candidate manifest changed before fake LFS readback")
        source = manifest_path.parent / "data/data-go-kr.registry.json"
        payload = source.read_bytes()
        if (len(payload), hashlib.sha256(payload).hexdigest()) != (expected_bytes, expected_sha256):
            raise AssertionError("fake LFS boundary did not receive exact candidate bytes")
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(payload)
        self.validate(destination, expected_bytes, expected_sha256)
        binding = self.tracked_policy_binding(policy_path, manifest_path.parent, candidate_commit)
        return {
            "readback": "isolated_lfs_storage_verified",
            **binding,
        }


class PromotionRepo:
    """Small disposable Git checkout that exercises the actual C caller safely."""

    def __init__(self, flow: Any, baseline_payload: bytes) -> None:
        self.flow = flow
        self.temp = tempfile.TemporaryDirectory(prefix="same-observation-c-delivery-")
        self.root = pathlib.Path(self.temp.name) / "registry"
        self.root.mkdir()
        self.main_payload = baseline_payload
        self.current_main = ""
        self.initial_manifest = b""
        self.initial_tracked: set[str] = set()
        self.pr_created = False
        self.current_candidate_receipt: dict[str, Any] | None = None
        self.canonical_parent_receipt: dict[str, Any] | None = None
        self.canonical_parent_pr_number: int | None = None
        self.issue_rows: list[dict[str, Any]] = []
        self.pr_number = 9901
        self.pr_create_count = 0
        self.synthetic_merge_sha = "f" * 40
        self.state_conflict = False
        self.push_count = 0
        self._copy_fixture_tree()
        self._commit_baseline()
        self.materializer = FakeGitHubLfsMaterializer(self.root, self)

    def close(self) -> None:
        self.temp.cleanup()

    def _copy_path(self, relative: str) -> None:
        source = ROOT / relative
        target = self.root / relative
        if source.is_dir():
            shutil.copytree(source, target, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        elif source.is_file():
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)

    def _copy_fixture_tree(self) -> None:
        for relative in ("scripts", "schemas", "policy", ".gitattributes", ".gitignore", "manifest.json"):
            self._copy_path(relative)
        for relative in (
            ".github/workflows/upstream-catalogue-process.yml",
            ".github/workflows/upstream-catalog-refresh.yml",
            ".gira/config.yaml",
            "data/provider-index.json",
            "reports/latest-verification.json",
            "reports/health-probe-catalog.json",
            "reports/credential-runtime-manual-review-acceptance.json",
            "reports/diagnostic-current-source-applicability.json",
            "reports/unadapted-external-probe.json",
        ):
            self._copy_path(relative)
        source_policy = self.root / "policy/source-refresh.json"
        source_policy.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(self.flow.helper.policy_path, source_policy)

        data_path = self.root / "data/data-go-kr.registry.json"
        data_path.parent.mkdir(parents=True, exist_ok=True)
        data_path.write_bytes(registry_pointer(self.main_payload))
        manifest_path = self.root / "manifest.json"
        manifest = read_json(manifest_path)
        rows = [
            item for item in manifest.get("artifacts", [])
            if isinstance(item, dict) and item.get("kind") == "registry" and item.get("path") == "data/data-go-kr.registry.json"
        ]
        if len(rows) != 1:
            raise AssertionError("source manifest does not bind one canonical registry row")
        rows[0]["bytes"] = len(self.main_payload)
        rows[0]["sha256"] = hashlib.sha256(self.main_payload).hexdigest()
        manifest_path.write_bytes(json.dumps(manifest, ensure_ascii=False, indent=2).encode("utf-8") + b"\n")
        current = self.root / ".datapan/current-canonical/data/data-go-kr.registry.json"
        current.parent.mkdir(parents=True, exist_ok=True)
        current.write_bytes(self.main_payload)

    def _commit_baseline(self) -> None:
        subprocess.run(("git", "init", "--quiet", "--initial-branch=main"), cwd=self.root, check=True)
        subprocess.run(("git", "config", "user.name", "C delivery test"), cwd=self.root, check=True)
        subprocess.run(("git", "config", "user.email", "c-delivery-test@example.invalid"), cwd=self.root, check=True)
        subprocess.run(("git", "lfs", "install", "--local"), cwd=self.root, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        subprocess.run(("git", "remote", "add", "origin", "https://github.com/StatPan/datapan-registry.git"), cwd=self.root, check=True)
        subprocess.run(("git", "add", "-A"), cwd=self.root, check=True)
        subprocess.run(("git", "commit", "--quiet", "-m", "synthetic C0 canonical fixture"), cwd=self.root, check=True)
        self.current_main = subprocess.run(
            ("git", "rev-parse", "HEAD"), cwd=self.root, check=True,
            stdout=subprocess.PIPE, text=True,
        ).stdout.strip()
        self.initial_manifest = (self.root / "manifest.json").read_bytes()
        self.initial_tracked = {
            path.relative_to(self.root).as_posix()
            for path in self.root.rglob("*")
            if path.is_file() and ".git" not in path.relative_to(self.root).parts
        }

    def update_canonical_baseline(self, payload: bytes, main_sha: str) -> None:
        """Model an exact fast-forward C merge in this temporary repository."""
        self.main_payload = payload
        self.current_main = main_sha
        current = self.root / ".datapan/current-canonical/data/data-go-kr.registry.json"
        current.parent.mkdir(parents=True, exist_ok=True)
        current.write_bytes(payload)

    def command(self, argv: Any, cwd: pathlib.Path, **kwargs: Any) -> subprocess.CompletedProcess[str]:
        values = tuple(str(part) for part in argv)
        allowed = kwargs.get("allowed_returncodes", frozenset({0}))
        if cwd == self.root and values[:2] == ("git", "ls-remote"):
            ref = values[-1]
            if ref == "refs/heads/main":
                return subprocess.CompletedProcess(argv, 0, f"{self.current_main}\t{ref}\n", "")
            return subprocess.CompletedProcess(argv, 0, "", "")
        if cwd == self.root and values[:3] == ("git", "merge-base", "--is-ancestor"):
            ancestor, descendant = values[3], values[4]
            if ancestor == self.flow.observation_head or ancestor == self.synthetic_merge_sha:
                return subprocess.CompletedProcess(argv, 0, "", "")
            return subprocess.run(
                values, cwd=cwd, text=True, capture_output=True, check=False,
            )
        if cwd == self.root and values[:2] == ("git", "show") and len(values) == 3:
            spec = values[2]
            revision, separator, relative = spec.partition(":")
            if separator and revision == self.flow.observation_head:
                path = self.root / relative
                return subprocess.CompletedProcess(argv, 0, path.read_text(encoding="utf-8"), "")
        if any(part.endswith("materialize-canonical-registry.py") for part in values):
            output = pathlib.Path(values[values.index("--output") + 1])
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_bytes(self.main_payload)
            return subprocess.CompletedProcess(argv, 0, "", "")
        if values and values[0] in {"python3", os.sys.executable}:
            return subprocess.CompletedProcess(argv, 0, "", "")
        if values[:3] == ("gh", "issue", "create"):
            url = f"https://github.com/StatPan/datapan-registry/issues/{self.pr_number + 100}"
            body_path = pathlib.Path(values[values.index("--body-file") + 1])
            body = body_path.read_text(encoding="utf-8")
            marker = body.splitlines()[0]
            self.issue_rows.append({
                "number": self.pr_number + 100, "url": url, "state": "OPEN", "body": body,
                "marker": marker,
            })
            return subprocess.CompletedProcess(argv, 0, url + "\n", "")
        if values[:3] == ("gh", "pr", "create"):
            self.pr_create_count += 1
            if self.pr_create_count > 1:
                self.pr_number = 9900 + self.pr_create_count
            self.pr_created = True
            return subprocess.CompletedProcess(argv, 0, "", "")
        if values[:2] == ("gh", "api"):
            return subprocess.CompletedProcess(argv, 0, "[]", "")
        if values and values[0] == "gh":
            return subprocess.CompletedProcess(argv, 0, "[]", "")
        result = subprocess.run(values, cwd=cwd, text=True, capture_output=True, check=False, env=kwargs.get("env"))
        if result.returncode not in allowed:
            raise PROMOTION.PromotionError(f"test Git adapter unexpected command failure: {values!r}")
        return result

    def open_pr_rows(self, _root: pathlib.Path, _repository: str) -> list[dict[str, Any]]:
        if not self.pr_created or self.current_candidate_receipt is None:
            return []
        receipt = self.current_candidate_receipt
        candidate = receipt["candidate"]
        ownership = receipt["ownership"]
        url = f"https://github.com/StatPan/datapan-registry/pull/{self.pr_number}"
        return [{
            "repository": "StatPan/datapan-registry",
            "source_id": candidate["source_id"], "scope": candidate["scope"],
            "state": "open", "owner_id": ownership["owner_id"], "number": self.pr_number,
            "url": url, "head_sha": candidate["head_sha"],
            "automation_head_sha": candidate["head_sha"],
            "body": ownership["body"], "body_sha256": ownership["body_sha256"],
            "automation_body_sha256": ownership["body_sha256"],
            "head_ref": ownership["branch"], "base_ref": "main",
            "candidate_head_sha": candidate["head_sha"],
            "manifest_sha256": candidate["manifest_sha256"],
            "registry_sha256": candidate["registry_sha256"],
            "generation_id": candidate["generation_id"],
        }]

    def pr_readback(self, _root: pathlib.Path, _repository: str, number: int) -> dict[str, Any]:
        if self.canonical_parent_receipt is not None and number == self.canonical_parent_pr_number:
            row = self.canonical_parent_receipt
            candidate = row["candidate"]
            ownership = row["ownership"]
            pr = row["pr"]
            return {
                "number": number,
                "url": f"https://github.com/StatPan/datapan-registry/pull/{number}",
                "state": "CLOSED", "merged": True, "repository": "StatPan/datapan-registry",
                "headRepository": "StatPan/datapan-registry", "headRefName": ownership["branch"],
                "headRefOid": candidate["head_sha"], "baseRefName": "main",
                "mergeCommit": {"oid": pr["merge_commit_sha"]}, "body": ownership["body"],
            }
        receipt = self.current_candidate_receipt
        if receipt is None:
            raise AssertionError("PR readback requested before the test fixture prepared a receipt")
        candidate = receipt["candidate"]
        ownership = receipt["ownership"]
        return {
            "number": number, "url": f"https://github.com/StatPan/datapan-registry/pull/{number}",
            "state": "OPEN", "repository": "StatPan/datapan-registry",
            "headRepository": "StatPan/datapan-registry", "headRefName": ownership["branch"],
            "headRefOid": candidate["head_sha"], "baseRefName": "main",
            "mergeCommit": None, "body": ownership["body"],
        }


class SameObservationCDeliveryTests(unittest.TestCase):
    """Run the ordinary derived B → C delivery/readback path with local boundaries."""

    @mock.patch.dict(os.environ, {"GIT_LFS_SKIP_SMUDGE": "1"})
    def test_changed_b2_delivery_emits_real_c_reducers_and_b3_replay_is_idempotent(self) -> None:
        flow = FLOW_TESTS.SameObservationProcessorFlowTests()
        flow.setUp()
        repo: PromotionRepo | None = None
        try:
            # Candidate promotion must validate a nonempty canonical baseline.
            # Retain one already-enriched LINK baseline row so it does not
            # consume a detail slot and only the synthetic identities queue.
            candidate_rows = read_json(flow.helper.candidate_path)
            baseline_row = copy.deepcopy(flow.helper.old_link)
            baseline_row["id"] = "0"
            baseline_row["priority"] = "medium"
            baseline_row.pop("type", None)
            baseline_row["source"]["url"] = "https://www.data.go.kr/data/0/openapi.do"
            baseline_row["source"]["raw"].update({
                "api_id": "0", "meta_url": "https://www.data.go.kr/data/0/openapi.do",
            })
            flow.helper.write_real_composer_inputs(
                [baseline_row], [baseline_row, *candidate_rows], flow.now_text,
            )
            flow.admission_path, flow.archive_path = flow._write_admission()

            # Keep C0 intentionally behind the observation: B0 accepts the
            # first queued guide, then derived B2 accepts the next one. Its
            # real C delivery advances main; B3 continues from that exact C1.
            b0 = flow._run_claim_and_worker(1, max_queue=1)
            journal0, c0 = flow._synthetic_c_journal(b0, 1)
            repo = PromotionRepo(flow, c0["payload"])
            repo.synthetic_merge_sha = c0["readback"]["merge_sha"]
            repo.canonical_parent_receipt = copy.deepcopy(journal0["records"][0])
            repo.canonical_parent_pr_number = int(journal0["records"][0]["pr"]["number"])
            c0["baseline"]["main_sha"] = repo.current_main
            c0["baseline"]["manifest_sha256"] = hashlib.sha256(repo.initial_manifest).hexdigest()
            flow.current_manifests[repo.current_main] = repo.initial_manifest

            plan2 = flow._prepare_authenticated_plan(b0, journal0, c0, label="c-delivery-b2")
            self.assertTrue(plan2["eligible"], plan2)
            env2 = FLOW_TESTS.read_json(pathlib.Path(plan2["derivation_path"]))
            self.assertEqual(env2["resume_parent_processor"]["generation_id"], b0["generation_id"])
            self.assertEqual(env2["canonical_parent_processor"]["generation_id"], b0["generation_id"])
            for field, checkpoint in (
                ("resume_parent_processor", b0),
                ("canonical_parent_processor", b0),
            ):
                with self.subTest(parent=field):
                    bundle = flow.output_by_generation[checkpoint["generation_id"]]
                    self.assertEqual(env2[field]["generation_id"], checkpoint["generation_id"])
                    for digest in checkpoint["output_digests"]:
                        member = bundle / digest["path"]
                        self.assertEqual(member.stat().st_size, digest["bytes"], str(member))
                        self.assertEqual(
                            hashlib.sha256(member.read_bytes()).hexdigest(), digest["sha256"], str(member),
                        )
            b2 = flow._run_claim_and_worker(
                2, derivation=env2,
                composition_baseline=pathlib.Path(plan2["composition_baseline_path"]),
                resume_parent_bundle=flow.output_by_generation[b0["generation_id"]],
                canonical_parent_bundle=flow.output_by_generation[b0["generation_id"]],
                journal=journal0, journal_ref_sha=c0["readback"]["journal_ref_sha"],
            )
            self._assert_payload_changed(flow, b0, b2)

            state = {"journal": journal0, "sha": c0["readback"]["journal_ref_sha"]}
            runner = self._runner_adapter(flow, repo, state)
            output_b2 = self._execute_candidate(flow, repo, state, runner, b2, c0)
            self.assertEqual(output_b2["status"], "pending-review")
            self.assertEqual(output_b2["pr_number"], repo.pr_number)
            self.assertEqual(len(state["journal"]["records"]), 2)
            pending_b2 = self._candidate_record(flow, state["journal"], b2["generation_id"])
            self.assertEqual(pending_b2["status"], "pending-review")
            self.assertEqual([ack["status"] for ack in pending_b2["acknowledgements"]], ["pending-review"])
            self.assertEqual(repo.push_count, 1)

            # Exact replay travels through the normal C caller on an exact main
            # checkout. Ownership/body/head validators remain live; the mocked
            # PR/API adapter returns only the fixture's existing row.
            subprocess.run(("git", "checkout", "--quiet", "--detach", repo.current_main), cwd=repo.root, check=True)
            repo.pr_created = True
            repo.current_candidate_receipt = copy.deepcopy(pending_b2)
            before_records = len(state["journal"]["records"])
            before_pushes = repo.push_count
            replay = self._execute_candidate(flow, repo, state, runner, b2, c0, output_root_label="b2-replay")
            self.assertEqual(replay["status"], "already-delivered")
            self.assertEqual(replay["pr_number"], repo.pr_number)
            self.assertEqual(len(state["journal"]["records"]), before_records)
            self.assertEqual(repo.push_count, before_pushes)

            # The actual PR readback reducer appends the merge acknowledgement;
            # the journal reducer preserves the exact prior pending-review ack.
            merged_sha = pending_b2["candidate"]["head_sha"]
            merged_readback = repo.pr_readback(repo.root, "StatPan/datapan-registry", repo.pr_number)
            merged_readback.update({"state": "MERGED", "merged": True, "mergeCommit": {"oid": merged_sha}})
            pending_ack_at = dt.datetime.fromisoformat(
                pending_b2["acknowledgements"][-1]["observed_at"].replace("Z", "+00:00"),
            ).astimezone(dt.timezone.utc)
            c1_observed_at = (pending_ack_at + dt.timedelta(seconds=1)).isoformat().replace("+00:00", "Z")
            c1_receipt = runner["helper"].record_pr_readback(
                pending_b2, merged_readback,
                observed_at=c1_observed_at,
                run_url="https://github.com/StatPan/datapan-registry/actions/runs/99000002001/attempts/1",
            )
            journal1 = runner["helper"].append_journal_record(
                state["journal"], c1_receipt,
                repository="StatPan/datapan-registry", observed_at=c1_observed_at,
            )
            runner["helper"].validate_journal(
                journal1, FLOW_TESTS.read_json(ROOT / "schemas/datapan.canonical-update-promotion-journal.v1.schema.json"),
            )
            state["journal"] = journal1
            state["sha"] = hashlib.sha1(DERIVATION.canonical_json(journal1)).hexdigest()
            repo.canonical_parent_receipt = copy.deepcopy(c1_receipt)
            repo.canonical_parent_pr_number = int(c1_receipt["pr"]["number"])
            repo.pr_created = False  # The exact B2 PR is merged, so no open row remains.
            repo.update_canonical_baseline(
                (flow.output_by_generation[b2["generation_id"]] / "composed-candidate.registry.json").read_bytes(),
                merged_sha,
            )
            subprocess.run(("git", "checkout", "--quiet", "--detach", merged_sha), cwd=repo.root, check=True)
            current_manifest = (repo.root / "manifest.json").read_bytes()
            flow.current_manifests[merged_sha] = current_manifest
            c1 = {
                "readback": DERIVATION.canonical_parent_readback_reference(
                    journal1, journal_ref_sha=state["sha"], record_index=1,
                ),
                "baseline": {
                    "main_sha": merged_sha,
                    "manifest_sha256": hashlib.sha256(current_manifest).hexdigest(),
                    "registry_path": DERIVATION.SAFE_PATH,
                    "registry_sha256": hashlib.sha256(repo.main_payload).hexdigest(),
                    "registry_bytes": len(repo.main_payload),
                    "payload": repo.main_payload,
                },
                "payload": repo.main_payload,
            }

            plan3 = flow._prepare_authenticated_plan(b2, journal1, c1, label="c-delivery-b3")
            self.assertTrue(plan3["eligible"], plan3)
            env3 = FLOW_TESTS.read_json(pathlib.Path(plan3["derivation_path"]))
            b3 = flow._run_claim_and_worker(
                3, derivation=env3,
                composition_baseline=pathlib.Path(plan3["composition_baseline_path"]),
                resume_parent_bundle=flow.output_by_generation[b2["generation_id"]],
                canonical_parent_bundle=flow.output_by_generation[b2["generation_id"]],
                journal=journal1, journal_ref_sha=state["sha"],
            )
            self._assert_payload_changed(flow, b2, b3)

            # A live C caller must reject a derivative whose exact canonical
            # parent PR identity no longer matches the durable journal, before
            # creating an issue/PR, pushing a branch, or appending an ACK.
            wrong_parent_journal = copy.deepcopy(journal1)
            wrong_parent_journal["records"][1]["pr"]["number"] += 10000
            runner["helper"].validate_journal(
                wrong_parent_journal,
                FLOW_TESTS.read_json(ROOT / "schemas/datapan.canonical-update-promotion-journal.v1.schema.json"),
            )
            original_journal = state["journal"]
            original_journal_sha = state["sha"]
            before_negative = (
                repo.pr_create_count, len(repo.issue_rows), repo.push_count,
                len(state["journal"]["records"]),
            )
            state["journal"] = wrong_parent_journal
            state["sha"] = hashlib.sha1(DERIVATION.canonical_json(wrong_parent_journal)).hexdigest()
            try:
                with self.assertRaisesRegex(PROMOTION.PromotionError, "strict C validation"):
                    self._execute_candidate(
                        flow, repo, state, runner, b3, c1, output_root_label="b3-wrong-c-parent",
                    )
            finally:
                state["journal"] = original_journal
                state["sha"] = original_journal_sha
            self.assertEqual(
                (repo.pr_create_count, len(repo.issue_rows), repo.push_count,
                 len(state["journal"]["records"])),
                before_negative,
            )

            output_b3 = self._execute_candidate(
                flow, repo, state, runner, b3, c1, output_root_label="b3-delivery",
            )
            self.assertEqual(output_b3["status"], "pending-review")
            pending_b3 = self._candidate_record(flow, state["journal"], b3["generation_id"])
            self.assertEqual([ack["status"] for ack in pending_b3["acknowledgements"]], ["pending-review"])
            self.assertEqual(repo.push_count, 2)
            self.assertEqual(len(state["journal"]["records"]), 3)

            # Re-running the same B3 candidate returns its exact existing PR;
            # it performs no branch push and appends no duplicate ACK or row.
            subprocess.run(("git", "checkout", "--quiet", "--detach", merged_sha), cwd=repo.root, check=True)
            repo.pr_created = True
            repo.current_candidate_receipt = copy.deepcopy(pending_b3)
            before_records = len(state["journal"]["records"])
            before_acks = copy.deepcopy(pending_b3["acknowledgements"])
            before_pushes = repo.push_count
            replay_b3 = self._execute_candidate(
                flow, repo, state, runner, b3, c1, output_root_label="b3-replay",
            )
            self.assertEqual(replay_b3["status"], "already-delivered")
            self.assertEqual(len(state["journal"]["records"]), before_records)
            self.assertEqual(self._candidate_record(flow, state["journal"], b3["generation_id"])["acknowledgements"], before_acks)
            self.assertEqual(repo.push_count, before_pushes)

            # Reduce the second real merge through the same PR readback and
            # journal reducers. This advances canonical C to B3's exact bytes.
            merged_b3_sha = pending_b3["candidate"]["head_sha"]
            merged_b3_readback = repo.pr_readback(repo.root, "StatPan/datapan-registry", repo.pr_number)
            merged_b3_readback.update({
                "state": "MERGED", "merged": True,
                "mergeCommit": {"oid": merged_b3_sha},
            })
            pending_b3_at = dt.datetime.fromisoformat(
                pending_b3["acknowledgements"][-1]["observed_at"].replace("Z", "+00:00"),
            ).astimezone(dt.timezone.utc)
            c2_observed_at = (pending_b3_at + dt.timedelta(seconds=1)).isoformat().replace("+00:00", "Z")
            c2_receipt = runner["helper"].record_pr_readback(
                pending_b3, merged_b3_readback,
                observed_at=c2_observed_at,
                run_url="https://github.com/StatPan/datapan-registry/actions/runs/99000002003/attempts/1",
            )
            journal2 = runner["helper"].append_journal_record(
                state["journal"], c2_receipt,
                repository="StatPan/datapan-registry", observed_at=c2_observed_at,
            )
            runner["helper"].validate_journal(
                journal2,
                FLOW_TESTS.read_json(ROOT / "schemas/datapan.canonical-update-promotion-journal.v1.schema.json"),
            )
            state["journal"] = journal2
            state["sha"] = hashlib.sha1(DERIVATION.canonical_json(journal2)).hexdigest()
            repo.canonical_parent_receipt = copy.deepcopy(c2_receipt)
            repo.canonical_parent_pr_number = int(c2_receipt["pr"]["number"])
            repo.current_candidate_receipt = copy.deepcopy(c2_receipt)
            repo.pr_created = False
            b3_payload = (
                flow.output_by_generation[b3["generation_id"]] / "composed-candidate.registry.json"
            ).read_bytes()
            repo.update_canonical_baseline(b3_payload, merged_b3_sha)
            subprocess.run(("git", "checkout", "--quiet", "--detach", merged_b3_sha), cwd=repo.root, check=True)
            current_manifest_b3 = (repo.root / "manifest.json").read_bytes()
            flow.current_manifests[merged_b3_sha] = current_manifest_b3
            c2 = {
                "readback": DERIVATION.canonical_parent_readback_reference(
                    journal2, journal_ref_sha=state["sha"], record_index=2,
                ),
                "baseline": {
                    "main_sha": merged_b3_sha,
                    "manifest_sha256": hashlib.sha256(current_manifest_b3).hexdigest(),
                    "registry_path": DERIVATION.SAFE_PATH,
                    "registry_sha256": hashlib.sha256(b3_payload).hexdigest(),
                    "registry_bytes": len(b3_payload),
                    "payload": b3_payload,
                },
                "payload": b3_payload,
            }

            # The now-canonical B3 candidate replays as a no-op. It must not
            # stage a tree change, reopen/create a PR, push, or append a row.
            before_canonical_replay = {
                "journal": copy.deepcopy(state["journal"]),
                "sha": state["sha"],
                "acks": copy.deepcopy(self._candidate_record(flow, state["journal"], b3["generation_id"])["acknowledgements"]),
                "pr_creates": repo.pr_create_count,
                "issues": len(repo.issue_rows),
                "pushes": repo.push_count,
                "head": subprocess.run(("git", "rev-parse", "HEAD"), cwd=repo.root, check=True,
                                       stdout=subprocess.PIPE, text=True).stdout.strip(),
                "tree": subprocess.run(("git", "write-tree"), cwd=repo.root, check=True,
                                       stdout=subprocess.PIPE, text=True).stdout.strip(),
                "status": subprocess.run(("git", "status", "--porcelain", "--untracked-files=all"),
                                          cwd=repo.root, check=True, stdout=subprocess.PIPE,
                                          text=True).stdout,
            }
            canonical_replay = self._execute_candidate(
                flow, repo, state, runner, b3, c2, output_root_label="b3-already-canonical",
            )
            self.assertEqual(canonical_replay["status"], "already-canonical-payload")
            self.assertFalse(canonical_replay["candidate_available"])
            self.assertEqual(state["journal"], before_canonical_replay["journal"])
            self.assertEqual(state["sha"], before_canonical_replay["sha"])
            self.assertEqual(
                self._candidate_record(flow, state["journal"], b3["generation_id"])["acknowledgements"],
                before_canonical_replay["acks"],
            )
            self.assertEqual(repo.pr_create_count, before_canonical_replay["pr_creates"])
            self.assertEqual(len(repo.issue_rows), before_canonical_replay["issues"])
            self.assertEqual(repo.push_count, before_canonical_replay["pushes"])
            self.assertEqual(
                subprocess.run(("git", "rev-parse", "HEAD"), cwd=repo.root, check=True,
                               stdout=subprocess.PIPE, text=True).stdout.strip(),
                before_canonical_replay["head"],
            )
            self.assertEqual(
                subprocess.run(("git", "write-tree"), cwd=repo.root, check=True,
                               stdout=subprocess.PIPE, text=True).stdout.strip(),
                before_canonical_replay["tree"],
            )
            self.assertEqual(
                subprocess.run(("git", "status", "--porcelain", "--untracked-files=all"),
                               cwd=repo.root, check=True, stdout=subprocess.PIPE,
                               text=True).stdout,
                before_canonical_replay["status"],
            )

            self.assertEqual(
                [row["status"] for row in state["journal"]["records"]],
                ["merged", "merged", "merged"],
            )
            self.assertEqual(
                [ack["status"] for ack in self._candidate_record(flow, state["journal"], b2["generation_id"])["acknowledgements"]],
                ["pending-review", "merged"],
            )
            self.assertEqual(
                [ack["status"] for ack in self._candidate_record(flow, state["journal"], b3["generation_id"])["acknowledgements"]],
                ["pending-review", "merged"],
            )
            self.assertEqual(repo.pr_create_count, 2)
            self.assertEqual(len(flow.detail_calls), 3)
            selected_detail_ids = [
                url.split("/data/", 1)[1].split("/", 1)[0] for url in flow.detail_calls
            ]
            self.assertEqual(len(selected_detail_ids), 3)
            self.assertEqual(len(set(selected_detail_ids)), 3)
            self.assertCountEqual(selected_detail_ids, ["1", "2", "3"])
        finally:
            if repo is not None:
                repo.close()
            flow.tearDown()

    def _assert_payload_changed(self, flow: Any, baseline: dict[str, Any], candidate: dict[str, Any]) -> None:
        baseline_bytes = (flow.output_by_generation[baseline["generation_id"]] / "composed-candidate.registry.json").read_bytes()
        candidate_bytes = (flow.output_by_generation[candidate["generation_id"]] / "composed-candidate.registry.json").read_bytes()
        self.assertNotEqual(hashlib.sha256(baseline_bytes).hexdigest(), hashlib.sha256(candidate_bytes).hexdigest())

    def _candidate_record(self, flow: Any, journal: dict[str, Any], generation_id: str) -> dict[str, Any]:
        matches = [
            row for row in journal["records"]
            if row.get("candidate", {}).get("generation_id") == generation_id
        ]
        self.assertEqual(len(matches), 1)
        return matches[0]

    def _runner_adapter(self, flow: Any, repo: PromotionRepo, state: dict[str, Any]) -> dict[str, Any]:
        helper = PROMOTION.load_canonical_update_pr(repo.root)
        schema = FLOW_TESTS.read_json(ROOT / "schemas/datapan.canonical-update-promotion-journal.v1.schema.json")
        persistence_calls: list[str] = []

        def persist(_root: pathlib.Path, _base: str, receipt: dict[str, Any], *, observed_at: str,
                    expected_state_sha: Any = PROMOTION.STATE_EXPECTATION_UNSET, **kwargs: Any) -> str:
            del kwargs
            persistence_calls.append(str(receipt.get("status")))
            if expected_state_sha is not PROMOTION.STATE_EXPECTATION_UNSET and expected_state_sha != state["sha"]:
                raise PROMOTION.PromotionError("promotion state compare-and-swap conflict")
            if repo.state_conflict:
                repo.state_conflict = False
                raise PROMOTION.PromotionError("promotion state compare-and-swap conflict")
            updated = helper.append_journal_record(
                state["journal"], receipt, repository="StatPan/datapan-registry", observed_at=observed_at,
            )
            helper.validate_journal(updated, schema)
            state["journal"] = updated
            state["sha"] = hashlib.sha1(DERIVATION.canonical_json(updated)).hexdigest()
            prepared = self._candidate_record(flow, updated, receipt["candidate"]["generation_id"])
            if prepared.get("status") == "prepared":
                repo.current_candidate_receipt = copy.deepcopy(prepared)
            return state["sha"]

        repo._state = state
        repo._persistence_calls = persistence_calls
        return {"helper": helper, "schema": schema, "persist": persist, "persistence_calls": persistence_calls}

    def _execute_candidate(
        self, flow: Any, repo: PromotionRepo, state: dict[str, Any], runner: dict[str, Any],
        checkpoint: dict[str, Any], c: dict[str, Any], *, output_root_label: str = "delivery",
    ) -> dict[str, Any]:
        del output_root_label
        bundle_dir = flow.output_by_generation[checkpoint["generation_id"]]
        run_id, attempt, _name = PROMOTION.processor_attempt_from_locator(checkpoint)
        args = type("CandidatePreparationArgs", (), {
            "workflow_run_id": run_id,
            "workflow_run_attempt": attempt,
            "workflow_run_head_sha": flow.observation_head,
            "bundle_dir": bundle_dir,
            "state_root": flow.helper.state_dir,
            "processor_artifact_id": checkpoint["output_artifact"]["artifact_id"],
            "source_refresh_predecessor": None,
            "source_refresh_target_main_sha": None,
            "source_refresh_expected_state_sha": None,
            "source_refresh_successor_head_sha": None,
            "datapan_cli": repo.root,
            "prepare_only": False,
        })()
        adapter = SourceRefreshTestAdapter()
        stdout = __import__("io").StringIO()
        original_load_module = PROMOTION.load_module

        def load_module(path: pathlib.Path, name: str) -> Any:
            if pathlib.Path(path).name == "refresh-canonical-snapshot-evidence.py":
                return adapter
            return original_load_module(path, name)

        original_ls_remote = repo.current_main
        issue_list_calls = 0

        def gh_json(_root: pathlib.Path, *argv: str) -> Any:
            nonlocal issue_list_calls
            if argv[:2] == ("issue", "list"):
                issue_list_calls += 1
                return copy.deepcopy(repo.issue_rows)
            if argv[:2] == ("issue", "view"):
                number = int(argv[2])
                return next(row for row in repo.issue_rows if row["number"] == number)
            if argv[:2] == ("pr", "view"):
                return repo.pr_readback(repo.root, "StatPan/datapan-registry", int(argv[2]))
            raise AssertionError(f"unexpected external GitHub read in C adapter: {argv!r}")

        selected_row = state["journal"]["records"][c["readback"]["journal_record_index"]]
        merged_ack = next(
            row for row in selected_row["acknowledgements"] if row.get("status") == "merged"
        )
        health_policy = read_json(repo.root / "policy/upstream-catalogue-health.json")
        promotion_path = health_policy["promotion_state"]["promotion_workflow_path"]
        ack_run_id = str(merged_ack["run_id"])
        ack_attempt = int(merged_ack["run_attempt"])
        ack_completed_at = merged_ack["observed_at"]
        ack_source_sha = merged_ack["source_sha"]
        original_subprocess_run = subprocess.run

        def github_health_transport(argv: Any, *args: Any, **kwargs: Any) -> subprocess.CompletedProcess[str]:
            values = tuple(str(part) for part in argv)
            if values[:2] == ("gh", "api"):
                endpoint = values[2]
                if endpoint == "repos/StatPan/datapan-registry/actions/workflows/canonical-update-promotion.yml":
                    value: Any = {"id": 912345, "path": promotion_path}
                elif endpoint == f"repos/StatPan/datapan-registry/actions/runs/{ack_run_id}/attempts/{ack_attempt}":
                    value = {
                        "id": int(ack_run_id), "run_attempt": ack_attempt,
                        "workflow_id": 912345, "path": promotion_path,
                        "event": "workflow_run", "head_branch": "main", "head_sha": ack_source_sha,
                        "status": "completed", "conclusion": "success",
                        "completed_at": ack_completed_at,
                        "repository": {"full_name": "StatPan/datapan-registry"},
                        "head_repository": {"full_name": "StatPan/datapan-registry"},
                    }
                elif endpoint.startswith(
                    f"repos/StatPan/datapan-registry/actions/runs/{ack_run_id}/attempts/{ack_attempt}/jobs?"
                ):
                    value = {
                        "total_count": 1,
                        "jobs": [{
                            "id": 912346, "run_id": int(ack_run_id), "run_attempt": ack_attempt,
                            "head_sha": ack_source_sha, "status": "completed", "conclusion": "success",
                            "completed_at": ack_completed_at,
                        }],
                    }
                else:
                    raise AssertionError(f"unexpected strict C Health API request: {endpoint}")
                return subprocess.CompletedProcess(argv, 0, json.dumps(value), "")
            return original_subprocess_run(argv, *args, **kwargs)

        patches = (
            mock.patch.dict(PROMOTION.os.environ, {
                "GITHUB_REPOSITORY": "StatPan/datapan-registry",
                "GITHUB_RUN_ID": "99000002002", "GITHUB_RUN_ATTEMPT": "1",
            }),
            mock.patch.object(PROMOTION, "command", side_effect=repo.command),
            mock.patch.object(PROMOTION, "load_module", side_effect=load_module),
            mock.patch.object(PROMOTION, "load_canonical_update_pr", return_value=runner["helper"]),
            mock.patch.object(PROMOTION, "load_promotion_journal_snapshot", return_value=(state["journal"], state["sha"])),
            mock.patch.object(PROMOTION, "persist_journal_record", side_effect=runner["persist"]),
            mock.patch.object(PROMOTION, "gh_open_prs", side_effect=repo.open_pr_rows),
            mock.patch.object(PROMOTION, "gh_pr_readback", side_effect=repo.pr_readback),
            mock.patch.object(PROMOTION, "gh_json", side_effect=gh_json),
            mock.patch.object(PROMOTION, "verify_release_ci_observation", return_value=(None, "test adapter: CI not exercised")),
            mock.patch.object(runner["helper"], "load_materializer", return_value=repo.materializer),
            mock.patch.object(subprocess, "run", side_effect=github_health_transport),
        )
        with contextlib.ExitStack() as stack:
            for patch in patches:
                stack.enter_context(patch)
            with (
                mock.patch.object(__import__("sys"), "argv", []),
                contextlib.redirect_stdout(stdout),
                contextlib.chdir(repo.root),
            ):
                PROMOTION.execute_candidate_preparation(args, repo.root)
        lines = [line for line in stdout.getvalue().splitlines() if line.startswith("{")]
        self.assertEqual(len(lines), 1, stdout.getvalue())
        return json.loads(lines[0])


if __name__ == "__main__":
    unittest.main()
