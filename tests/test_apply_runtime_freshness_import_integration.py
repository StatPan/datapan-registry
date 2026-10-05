from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

import jsonschema


ROOT = pathlib.Path(__file__).parents[1]
SCRIPT = ROOT / "scripts/apply-runtime-freshness-import.py"
SPEC = importlib.util.spec_from_file_location("apply_runtime_freshness_import_integration", SCRIPT)
assert SPEC and SPEC.loader
APPLY = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(APPLY)
sys.path.insert(0, str(ROOT / "scripts"))
import runtime_evidence_projection as projection_lib  # noqa: E402


def run(command: list[str], *, cwd: pathlib.Path, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, cwd=cwd, env=env, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: pathlib.Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def identity_digest(identities: set[str]) -> str:
    return sha256_bytes(json.dumps(sorted(identities), ensure_ascii=False, separators=(",", ":")).encode("utf-8"))


def write_json(path: pathlib.Path, value: object) -> bytes:
    content = (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return content


class ApplyRuntimeFreshnessImportIntegrationTest(unittest.TestCase):
    """Run the actual transaction and generators; adapt only the external CLI."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.temporary = tempfile.TemporaryDirectory(prefix="runtime-import-736-integration-")
        cls.temp_root = pathlib.Path(cls.temporary.name)
        cls.repo = cls.temp_root / "repo"
        cls.adapter = cls.temp_root / "datapan-test-adapter.py"
        cls.command_log = cls.temp_root / "external-cli.jsonl"
        cls.report = cls.temp_root / "sanitized-report.json"
        cls.receipt = cls.temp_root / "run-receipt.json"
        cls.run_id = "fixture-736-20261005"
        cls._create_offline_repo()
        cls._create_external_cli_adapter()
        cls._create_sanitized_inputs()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.temporary.cleanup()

    @classmethod
    def _git(cls, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
        result = run(["git", *args], cwd=cls.repo)
        if check and result.returncode:
            raise AssertionError(f"git {' '.join(args)} failed: {result.stderr[-2000:]}")
        return result

    @classmethod
    def _create_offline_repo(cls) -> None:
        # Copy the exact local source commit, then use the already cached LFS
        # payload as an ordinary test-only blob. The real materializer therefore
        # reuses local bytes and never contacts a provider.
        result = run(["git", "clone", "--shared", "--no-checkout", "--quiet", str(ROOT), str(cls.repo)], cwd=cls.temp_root)
        if result.returncode:
            raise AssertionError(f"local fixture clone failed: {result.stderr[-2000:]}")
        checkout = run(
            ["git", "checkout", "--detach", "HEAD"], cwd=cls.repo,
            env={**os.environ, "GIT_LFS_SKIP_SMUDGE": "1"},
        )
        if checkout.returncode:
            raise AssertionError(f"local fixture checkout failed: {checkout.stderr[-2000:]}")

        pointer = subprocess.run(
            ["git", "show", "HEAD:data/data-go-kr.registry.json"], cwd=ROOT,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True,
        ).stdout
        cls.original_pointer_sha256 = sha256_bytes(pointer)
        lines = pointer.decode("ascii").splitlines()
        oid = next(line.partition(":")[2] for line in lines if line.startswith("oid sha256:"))
        size = int(next(line.partition(" ")[2] for line in lines if line.startswith("size ")))
        common = run(["git", "rev-parse", "--git-common-dir"], cwd=ROOT).stdout.strip()
        common_path = pathlib.Path(common)
        if not common_path.is_absolute():
            common_path = (ROOT / common_path).resolve()
        source_registry = ROOT / "data/data-go-kr.registry.json"
        if source_registry.is_file() and source_registry.stat().st_size == size and sha256_file(source_registry) == oid:
            payload = source_registry
        else:
            payload = common_path / "lfs/objects" / oid[:2] / oid[2:4] / oid
            if not payload.is_file() or payload.stat().st_size != size or sha256_file(payload) != oid:
                raise AssertionError("exact canonical Registry bytes are unavailable locally")
        registry = cls.repo / "data/data-go-kr.registry.json"
        shutil.copyfile(payload, registry)

        attributes = cls.repo / ".gitattributes"
        kept = [line for line in attributes.read_text(encoding="utf-8").splitlines()
                if not line.startswith("data/data-go-kr.registry.json ")]
        attributes.write_text("\n".join(kept) + "\n", encoding="utf-8")
        policy_path = cls.repo / "policy/registry-distribution.json"
        policy = json.loads(policy_path.read_text(encoding="utf-8"))
        policy["canonical_registry"]["manifest_sha256"] = oid
        write_json(policy_path, policy)

        # Include the current caller under test if it is still locally modified.
        shutil.copyfile(SCRIPT, cls.repo / "scripts/apply-runtime-freshness-import.py")
        cls._git("config", "user.name", "Codex Runtime Import Fixture")
        cls._git("config", "user.email", "codex-runtime-fixture@example.invalid")
        cls._git("add", ".gitattributes", "data/data-go-kr.registry.json",
                 "policy/registry-distribution.json", "scripts/apply-runtime-freshness-import.py")
        cls._git("commit", "-m", "Prepare local-only runtime import fixture")
        cls.fixture_revision = cls._git("rev-parse", "HEAD").stdout.strip()

        materialized = run([sys.executable, "scripts/materialize-canonical-registry.py"], cwd=cls.repo)
        if materialized.returncode or '"status": "reused"' not in materialized.stdout:
            raise AssertionError(
                "real materializer did not reuse the local Registry payload; "
                f"stdout={materialized.stdout[-1000:]} stderr={materialized.stderr[-1000:]}"
            )
        if registry.stat().st_size != size or sha256_file(registry) != oid:
            raise AssertionError("offline fixture Registry bytes differ from the source LFS payload")
        if cls._git("status", "--porcelain").stdout.strip():
            raise AssertionError("offline fixture baseline is not clean")

    @classmethod
    def _create_external_cli_adapter(cls) -> None:
        cls.adapter.write_text(
            """#!/usr/bin/env python3
import collections
import json
import os
import pathlib
import sys

args = sys.argv[1:]
with pathlib.Path(os.environ["DATAPAN_TEST_LOG"]).open("a", encoding="utf-8") as stream:
    stream.write(json.dumps(args) + "\\n")
output = pathlib.Path(args[args.index("--output") + 1])
if args[:3] == ["catalog", "verify", "merge"]:
    inputs = [pathlib.Path(args[i + 1]) for i, value in enumerate(args) if value == "--input"]
    merged = json.loads(inputs[0].read_text(encoding="utf-8"))
    merged["results"] = [row for path in inputs for row in json.loads(path.read_text(encoding="utf-8"))["results"]]
    output.write_text(json.dumps(merged, ensure_ascii=False, indent=2) + "\\n", encoding="utf-8")
elif args[:3] == ["catalog", "verify", "summary"]:
    source = pathlib.Path(args[args.index("--input") + 1])
    value = json.loads(source.read_text(encoding="utf-8"))
    rows = value["results"]
    counts = collections.Counter(row.get("status") if row.get("status") in {"verified", "failed", "skipped"} else "unknown" for row in rows)
    def groups(field, extra=None, allowed=None):
        counter = collections.Counter()
        for row in rows:
            key = row.get(field)
            if isinstance(key, str) and key and (allowed is None or key in allowed):
                counter[key] += 1
        return [{"key": key, "count": count, **({extra: key} if extra else {})} for key, count in sorted(counter.items())]
    summary = {
        "generated_at": value["generated_at"], "source": source.as_posix(),
        "provider": "data.go.kr", "registry": "data/data-go-kr.registry.json",
        "limit": 0, "truncated": False,
        "summary": {"total": len(rows), **{key: counts[key] for key in ("verified", "failed", "skipped", "unknown")}},
        "groups": {
            "by_status": groups("status", "status", {"verified", "failed", "skipped", "unknown"}),
            "by_reason": groups("reason", "reason"),
            "by_provider": groups("provider", "provider"),
            "by_endpoint_host": groups("endpoint_host", "host"),
            "by_kind": groups("dependency_class", "kind", {"data_go_kr_gateway", "external_endpoint", "service_root", "no_endpoint", "malformed_endpoint", "soap", "wms"}),
        },
    }
    output.write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\\n", encoding="utf-8")
else:
    raise SystemExit("unexpected external CLI command")
""",
            encoding="utf-8",
        )

    @classmethod
    def _create_sanitized_inputs(cls) -> None:
        latest = json.loads((cls.repo / "reports/latest-verification.json").read_text(encoding="utf-8"))
        projection = json.loads((cls.repo / "reports/current-runtime-evidence-projection.json").read_text(encoding="utf-8"))
        cls.admitted_runs_before = projection["provenance"]["admitted_runs"]
        cls.pending_attestation_runs_before = projection["provenance"]["pending_attestation_runs"]
        manifest_path = cls.repo / "reports/data-go-kr/operation-manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        generated = datetime.fromisoformat(latest["generated_at"].replace("Z", "+00:00")) + timedelta(days=1)
        generated_at = generated.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        verified_at = (generated - timedelta(minutes=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
        expired_at = (generated - timedelta(days=120)).strftime("%Y-%m-%dT%H:%M:%SZ")
        operations = [
            row for row in projection["operations"]
            if row.get("contract_complete") is True and row.get("upstream_operation_key")
        ]
        if len(operations) < 3:
            raise AssertionError("source fixture does not contain three complete operation contracts")
        source_snapshot = manifest["source_snapshot"]["sha256"]
        manifest_sha = sha256_bytes(manifest_path.read_bytes())
        results: list[dict[str, object]] = []
        for index, operation in enumerate(operations[:3]):
            plan_binding = projection_lib.make_contract_binding(
                operation,
                source_snapshot_sha256=source_snapshot,
                operation_manifest_sha256=manifest_sha,
            )
            binding = projection_lib.bind_result_to_plan(
                plan_binding,
                run_id=cls.run_id,
                plan_sha256=hashlib.sha256(f"{cls.run_id}:{operation['identity_key']}".encode()).hexdigest(),
            )
            if index == 1:
                binding["contract_sha256"] = "0" * 64
            endpoint = operation.get("endpoint")
            result: dict[str, object] = {
                "identity_key": operation["identity_key"],
                "contract_binding": binding,
                "dataset_id": operation["dataset_id"],
                "title": operation["operation"],
                "operation": operation["operation"],
                "provider": "data.go.kr",
                "dependency_class": "data_go_kr_gateway",
                "status": "verified",
                "reason": "offline integration fixture only",
                "verified_at": expired_at if index == 2 else verified_at,
            }
            if isinstance(endpoint, dict) and isinstance(endpoint.get("host"), str):
                result["endpoint_host"] = endpoint["host"]
            results.append(result)

        report_bytes = write_json(cls.report, {"generated_at": generated_at, "results": results})
        identities = {str(row["identity_key"]) for row in results}
        identity_set = {"count": len(identities), "sha256": identity_digest(identities)}
        write_json(cls.receipt, {
            "run_id": cls.run_id,
            "generated_at": generated_at,
            "summary": {
                "planned_operations": len(results), "reported_results": len(results),
                "verified": len(results), "failed": 0, "skipped": 0, "unknown": 0,
            },
            "identity_equality": {
                "identity_algorithm": "data_go_kr.batch-plan-identity-key.v1",
                "result_identity_field": "identity_key",
                "result_mapping_fields": ["dataset_id", "operation"],
                "digest_algorithm": "sha256-canonical-json-array.v1",
                "planned": identity_set, "reported": identity_set, "equal": True,
            },
            "combined_verification": {"bytes": len(report_bytes), "sha256": sha256_bytes(report_bytes)},
            "redaction": {
                "secret_values_present": False, "secret_hashes_present": False,
                "request_urls_present": False, "response_bodies_present": False,
            },
        })
        cls.expected_results = results
        cls.expected_ids = identities
        cls.generated_at = generated_at

    def _producer(self) -> dict[str, str]:
        return {
            "repository": "StatPan/datapan-registry", "revision": self.fixture_revision,
            "run_id": self.run_id,
            "run_url": f"https://github.com/StatPan/datapan-registry/actions/runs/{self.run_id}",
        }

    def _env(self) -> dict[str, str]:
        return {
            **os.environ,
            "DATAPAN_TEST_LOG": str(self.command_log),
            "PYTHONDONTWRITEBYTECODE": "1",
        }

    def _datapan_command(self) -> str:
        return f"{sys.executable} {self.adapter}"

    def _run_old_order_until_coverage(self) -> subprocess.CompletedProcess[str]:
        worktree = self.temp_root / "old-order-worktree"
        self._git("-c", "core.hooksPath=/dev/null", "worktree", "add", "--detach", str(worktree), "HEAD")
        proposal = pathlib.Path(".datapan/runtime-freshness/import-proposal.json")
        import_receipt = pathlib.Path(f"reports/runtime-freshness-imports/{self.run_id}.json")
        admission = pathlib.Path(f"reports/runtime-freshness-import-admissions/{self.run_id}.json")
        producer = self._producer()
        try:
            imported = run(
                [sys.executable, "scripts/import-runtime-freshness-run.py", "--report", str(self.report),
                 "--receipt", str(self.receipt), "--datapan-command", self._datapan_command(),
                 "--proposal-output", proposal.as_posix(), "--apply"],
                cwd=worktree, env=self._env(),
            )
            self.assertEqual(imported.returncode, 0, imported.stderr)
            recovery = run(
                [sys.executable, "scripts/project-runtime-freshness-recovery.py",
                 "--report", str(self.report), "--run-receipt", str(self.receipt),
                 "--receipt-output", import_receipt.as_posix()],
                cwd=worktree,
            )
            self.assertEqual(recovery.returncode, 0, recovery.stderr)
            admission_command = [
                sys.executable, "scripts/generate-runtime-freshness-import-admission.py", "--root", ".",
                "--proposal", proposal.as_posix(), "--report", str(self.report), "--receipt", str(self.receipt),
                "--import-receipt", import_receipt.as_posix(), "--producer-repository", producer["repository"],
                "--producer-revision", producer["revision"], "--producer-run-id", self.run_id,
                "--producer-run-url", producer["run_url"], "--output", admission.as_posix(),
            ]
            admitted = run(admission_command, cwd=worktree)
            self.assertEqual(admitted.returncode, 0, admitted.stderr)
            growth = run([sys.executable, "scripts/generate-runtime-evidence-growth.py"], cwd=worktree)
            self.assertEqual(growth.returncode, 0, growth.stderr)
            materialized = run([sys.executable, "scripts/materialize-canonical-registry.py"], cwd=worktree)
            self.assertEqual(materialized.returncode, 0, materialized.stderr)
            return run([sys.executable, "scripts/generate-coverage-backlog.py"], cwd=worktree)
        finally:
            self._git("-c", "core.hooksPath=/dev/null", "worktree", "remove", "--force", str(worktree))

    def test_real_entrypoint_orders_projection_before_consumers_and_preserves_transaction(self) -> None:
        old_order = self._run_old_order_until_coverage()
        self.assertIn("current runtime evidence projection and latest verification evaluation times differ", old_order.stderr)
        self.assertEqual(self._git("status", "--porcelain").stdout.strip(), "")

        registry = self.repo / "data/data-go-kr.registry.json"
        registry_sha = hashlib.sha256(registry.read_bytes()).hexdigest()
        historical_receipts = {
            path.relative_to(self.repo).as_posix(): path.read_bytes()
            for path in (self.repo / "reports/runtime-freshness-imports").glob("*.json")
        }
        self.assertTrue(historical_receipts)
        unchanged_plans = {
            name: (self.repo / name).read_bytes()
            for name in (
                "reports/data-go-kr/operation-materialization-plan.json",
                "reports/data-go-kr/institution-runtime-plan.json",
            )
        }
        worktrees_before = self._git("worktree", "list", "--porcelain").stdout
        real_execute = APPLY.execute

        def fail_projection(command, *, cwd, capture=False, env=None):
            if any("generate-current-runtime-evidence-projection.py" in arg for arg in command):
                raise subprocess.CalledProcessError(1, command, stderr="injected projection failure")
            return real_execute(command, cwd=cwd, capture=True if not capture else capture, env=env)

        with mock.patch.dict(os.environ, {"DATAPAN_TEST_LOG": str(self.command_log)}), mock.patch.object(APPLY, "execute", side_effect=fail_projection):
            with self.assertRaises(subprocess.CalledProcessError) as failure:
                APPLY.apply_transaction(
                    self.repo, self.report, self.receipt, self._datapan_command(),
                    pathlib.Path(f"reports/runtime-freshness-imports/{self.run_id}.json"),
                    pathlib.Path(f"reports/runtime-freshness-import-admissions/{self.run_id}.json"),
                    self._producer(),
                )
        self.assertEqual(failure.exception.stderr, "injected projection failure")
        self.assertEqual(self._git("status", "--porcelain").stdout.strip(), "")
        self.assertEqual(self._git("worktree", "list", "--porcelain").stdout, worktrees_before)
        self.assertEqual(hashlib.sha256(registry.read_bytes()).hexdigest(), registry_sha)
        self.assertEqual({name: (self.repo / name).read_bytes() for name in historical_receipts}, historical_receipts)

        applied = run(
            [sys.executable, "scripts/apply-runtime-freshness-import.py", "--report", str(self.report),
             "--run-receipt", str(self.receipt), "--datapan-command", self._datapan_command(),
             "--producer-repository", self._producer()["repository"],
             "--producer-revision", self._producer()["revision"],
             "--producer-run-url", self._producer()["run_url"]],
            cwd=self.repo, env=self._env(),
        )
        self.assertEqual(applied.returncode, 0, applied.stderr[-4000:])
        self.assertIn('"status": "reused"', applied.stdout)
        result = json.loads(applied.stdout.strip().splitlines()[-1])
        self.assertEqual(result["status"], "applied")
        self.assertEqual(result["outcome"], "imported")

        expected_changed = {
            f"reports/runtime-freshness-imports/{self.run_id}.json",
            f"reports/runtime-freshness-import-admissions/{self.run_id}.json",
            "reports/latest-verification.json",
            "reports/latest-verification-summary.json",
            "reports/current-runtime-evidence-projection.json",
            "reports/data-go-kr/runtime-evidence-growth.json",
            "reports/data-go-kr/coverage-backlog.json",
            "reports/data-go-kr/institution-api-overview.json",
        }
        self.assertTrue(expected_changed.issubset(set(result["changed_files"]),), result["changed_files"])
        self.assertEqual(hashlib.sha256(registry.read_bytes()).hexdigest(), registry_sha)
        self.assertEqual({name: (self.repo / name).read_bytes() for name in historical_receipts}, historical_receipts)
        self.assertEqual({name: (self.repo / name).read_bytes() for name in unchanged_plans}, unchanged_plans)
        source_pointer = subprocess.run(
            ["git", "show", "HEAD:data/data-go-kr.registry.json"], cwd=ROOT,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True,
        ).stdout
        self.assertEqual(sha256_bytes(source_pointer), self.original_pointer_sha256)

        latest_path = self.repo / "reports/latest-verification.json"
        latest = json.loads(latest_path.read_text(encoding="utf-8"))
        projection_path = self.repo / "reports/current-runtime-evidence-projection.json"
        projection = json.loads(projection_path.read_text(encoding="utf-8"))
        schema = json.loads((self.repo / "schemas/datapan.current-runtime-evidence-projection.v1.schema.json").read_text(encoding="utf-8"))
        jsonschema.Draft202012Validator(schema, format_checker=jsonschema.FormatChecker()).validate(projection)
        self.assertEqual(latest["generated_at"], self.generated_at)
        self.assertEqual(projection["freshness"]["as_of"], latest["generated_at"])
        self.assertEqual(projection["inputs"]["registry_source_sha256"], registry_sha)
        manifest_path = self.repo / "reports/data-go-kr/operation-manifest.json"
        self.assertEqual(projection["inputs"]["operation_manifest_sha256"], sha256_bytes(manifest_path.read_bytes()))
        self.assertEqual(projection["inputs"]["latest_verification_sha256"], sha256_bytes(latest_path.read_bytes()))

        records = {row["identity_key"]: row for row in projection["records"] if row.get("identity_key") in self.expected_ids}
        self.assertEqual(set(records), self.expected_ids)
        changed_id = self.expected_results[1]["identity_key"]
        expired_id = self.expected_results[2]["identity_key"]
        fresh_id = self.expected_results[0]["identity_key"]
        self.assertEqual(records[fresh_id]["disposition"], "unbound")
        self.assertEqual(records[changed_id]["disposition"], "contract_changed")
        self.assertEqual(records[expired_id]["disposition"], "unbound")
        self.assertEqual(records[expired_id]["reason"], "immutable_import_receipt_binding_missing_or_mismatched")
        self.assertFalse(self.expected_ids.intersection(row["identity_key"] for row in projection["current_evidence"]))
        self.assertEqual(
            projection["provenance"]["admitted_runs"], self.admitted_runs_before + 1
        )
        self.assertEqual(
            projection["provenance"]["pending_attestation_runs"],
            self.pending_attestation_runs_before + 1,
        )

        expired_operation = next(row for row in projection["operations"] if row["identity_key"] == expired_id)
        expired_classification = projection_lib._disposition(
            self.expected_results[2], expired_operation, projection_lib.parse_time(self.generated_at),
            projection["freshness"]["fresh_days"], projection["freshness"]["expire_days"],
        )
        self.assertEqual(expired_classification[0], "expired")

        admission_path = self.repo / f"reports/runtime-freshness-import-admissions/{self.run_id}.json"
        admission = json.loads(admission_path.read_text(encoding="utf-8"))
        before, selected, after = (admission["arithmetic"][key] for key in ("before", "selected", "after"))
        self.assertEqual(after["total"], before["total"] + selected["total"])
        self.assertEqual(selected["total"], 3)
        self.assertEqual(selected["verified"], 3)
        self.assertEqual(admission["selected_identity_set"], {
            "count": len(self.expected_ids), "sha256": identity_digest(self.expected_ids),
        })

        growth_path = self.repo / "reports/data-go-kr/runtime-evidence-growth.json"
        backlog_path = self.repo / "reports/data-go-kr/coverage-backlog.json"
        growth = json.loads(growth_path.read_text(encoding="utf-8"))
        backlog = json.loads(backlog_path.read_text(encoding="utf-8"))
        self.assertEqual(growth["generated_at"], latest["generated_at"])
        self.assertEqual(growth["evidence"]["fresh_verified"], 0)
        self.assertEqual(backlog["generation_inputs"]["latest_verification"], "reports/latest-verification.json")

        stable_files = [projection_path, growth_path, backlog_path, self.repo / "reports/latest-verification-summary.json"]
        stable_hashes = {path: sha256_bytes(path.read_bytes()) for path in stable_files}
        check = run(
            [sys.executable, "scripts/refresh-release-ledger-evidence.py", "--check"],
            cwd=self.repo, env=self._env(),
        )
        self.assertEqual(check.returncode, 0, check.stdout[-2000:] + check.stderr[-2000:])
        self.assertEqual({path: sha256_bytes(path.read_bytes()) for path in stable_files}, stable_hashes)

        # Persist only in this disposable repo so replay is tested against the
        # exact durable admission. No commit or remote operation touches the source.
        self._git("add", "--", *result["changed_files"])
        self._git("commit", "-m", "Persist local fixture runtime import")
        replay_worktree_status = self._git("status", "--porcelain").stdout
        self.assertEqual(
            replay_worktree_status,
            "",
            f"fixture must be clean before exact replay: {replay_worktree_status}",
        )
        calls_before_replay = len(self.command_log.read_text(encoding="utf-8").splitlines())
        replay = run(
            [sys.executable, "scripts/apply-runtime-freshness-import.py", "--report", str(self.report),
             "--run-receipt", str(self.receipt), "--datapan-command", self._datapan_command(),
             "--producer-repository", self._producer()["repository"],
             "--producer-revision", self._producer()["revision"],
             "--producer-run-url", self._producer()["run_url"]],
            cwd=self.repo, env=self._env(),
        )
        self.assertEqual(replay.returncode, 0, replay.stderr)
        replay_value = json.loads(replay.stdout.strip().splitlines()[-1])
        self.assertEqual(replay_value["status"], "no_change")
        self.assertEqual(replay_value["changed_files"], [])
        self.assertEqual(len(self.command_log.read_text(encoding="utf-8").splitlines()), calls_before_replay)

        changed_report = json.loads(self.report.read_text(encoding="utf-8"))
        changed_report["results"][0]["reason"] = "different artifact bytes"
        changed_bytes = write_json(self.report, changed_report)
        changed_receipt = json.loads(self.receipt.read_text(encoding="utf-8"))
        changed_receipt["combined_verification"] = {"bytes": len(changed_bytes), "sha256": sha256_bytes(changed_bytes)}
        write_json(self.receipt, changed_receipt)
        rejected = run(
            [sys.executable, "scripts/apply-runtime-freshness-import.py", "--report", str(self.report),
             "--run-receipt", str(self.receipt), "--datapan-command", self._datapan_command(),
             "--producer-repository", self._producer()["repository"],
             "--producer-revision", self._producer()["revision"],
             "--producer-run-url", self._producer()["run_url"]],
            cwd=self.repo, env=self._env(),
        )
        self.assertNotEqual(rejected.returncode, 0)
        self.assertIn("different artifact bytes", rejected.stderr)
        self.assertEqual(len(self.command_log.read_text(encoding="utf-8").splitlines()), calls_before_replay)
        self.assertEqual(self._git("status", "--porcelain").stdout.strip(), "")


if __name__ == "__main__":
    unittest.main()
