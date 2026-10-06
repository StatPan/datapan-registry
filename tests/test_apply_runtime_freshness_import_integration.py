from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import pathlib
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from dataclasses import dataclass
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


CANONICAL_REGISTRY_PATH = "data/data-go-kr.registry.json"
LFS_POINTER_VERSION = "version https://git-lfs.github.com/spec/v1"
LOWER_SHA256 = re.compile(r"[0-9a-f]{64}\Z")


@dataclass(frozen=True)
class RegistryIdentity:
    path: str
    size: int
    sha256: str


def _unique_json_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    value: dict[str, object] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError(f"duplicate JSON object key: {key}")
        value[key] = item
    return value


def _reject_json_constant(value: str) -> object:
    raise ValueError(f"non-standard JSON constant: {value}")


def _decode_json_document(raw: bytes) -> object:
    try:
        return json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=_unique_json_object,
            parse_constant=_reject_json_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("invalid UTF-8 JSON document") from exc


def _registry_identity_from_manifest(manifest_bytes: bytes) -> RegistryIdentity:
    manifest = _decode_json_document(manifest_bytes)
    if not isinstance(manifest, dict):
        raise ValueError("committed manifest must be a JSON object")
    if manifest.get("source_registry") != CANONICAL_REGISTRY_PATH:
        raise ValueError("committed manifest does not name the canonical Registry path")
    artifacts = manifest.get("artifacts")
    if not isinstance(artifacts, list):
        raise ValueError("committed manifest artifacts must be an array")
    rows = [
        row for row in artifacts
        if isinstance(row, dict) and row.get("path") == CANONICAL_REGISTRY_PATH
    ]
    if len(rows) != 1:
        raise ValueError("committed manifest must contain exactly one canonical Registry artifact")
    row = rows[0]
    if row.get("kind") != "registry":
        raise ValueError("canonical Registry artifact has an unexpected kind")
    size = row.get("bytes")
    digest = row.get("sha256")
    if type(size) is not int or size <= 0:
        raise ValueError("canonical Registry artifact size must be a positive integer")
    if not isinstance(digest, str) or not LOWER_SHA256.fullmatch(digest):
        raise ValueError("canonical Registry artifact SHA256 must be lowercase hexadecimal")
    return RegistryIdentity(CANONICAL_REGISTRY_PATH, size, digest)


def _looks_like_lfs_pointer(raw: bytes) -> bool:
    first = raw.split(b"\n", 1)[0].lstrip()
    if first.startswith((b"[", b"{")):
        return False
    return (
        first.startswith((b"version ", b"oid "))
        or b"https://git-lfs.github.com/spec/v1" in raw[:512]
    )


def _parse_exact_lfs_pointer(raw: bytes) -> tuple[str, int]:
    try:
        lines = raw.decode("ascii").splitlines()
    except UnicodeDecodeError as exc:
        raise ValueError("LFS pointer must be ASCII") from exc
    if len(lines) != 3 or lines[0] != LFS_POINTER_VERSION:
        raise ValueError("LFS pointer must contain exactly the supported version, oid, and size lines")
    oid_prefix = "oid sha256:"
    size_prefix = "size "
    if not lines[1].startswith(oid_prefix) or not lines[2].startswith(size_prefix):
        raise ValueError("LFS pointer fields are missing, reordered, or ambiguous")
    oid = lines[1][len(oid_prefix):]
    size_text = lines[2][len(size_prefix):]
    if not LOWER_SHA256.fullmatch(oid):
        raise ValueError("LFS pointer oid must be lowercase SHA256")
    if not re.fullmatch(r"[0-9]+", size_text):
        raise ValueError("LFS pointer size must be a positive decimal integer")
    size = int(size_text)
    if size <= 0:
        raise ValueError("LFS pointer size must be a positive decimal integer")
    return oid, size


def _validate_json_array_of_objects(raw: bytes) -> None:
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError("materialized Registry must be UTF-8 JSON") from exc
    decoder = json.JSONDecoder(
        object_pairs_hook=_unique_json_object,
        parse_constant=_reject_json_constant,
    )
    index = 0

    def skip_space(position: int) -> int:
        while position < len(text) and text[position] in " \t\r\n":
            position += 1
        return position

    index = skip_space(index)
    if index >= len(text) or text[index] != "[":
        raise ValueError("materialized Registry must be a JSON array of objects")
    index += 1
    index = skip_space(index)
    if index < len(text) and text[index] == "]":
        index = skip_space(index + 1)
        if index != len(text):
            raise ValueError("materialized Registry has trailing data")
        return
    while True:
        index = skip_space(index)
        if index >= len(text) or text[index] != "{":
            raise ValueError("materialized Registry array entries must be JSON objects")
        try:
            value, index = decoder.raw_decode(text, index)
        except (json.JSONDecodeError, ValueError) as exc:
            raise ValueError("materialized Registry contains invalid JSON") from exc
        if not isinstance(value, dict):
            raise ValueError("materialized Registry array entries must be JSON objects")
        index = skip_space(index)
        if index < len(text) and text[index] == ",":
            index += 1
            continue
        if index < len(text) and text[index] == "]":
            index = skip_space(index + 1)
            if index != len(text):
                raise ValueError("materialized Registry has trailing data")
            return
        raise ValueError("materialized Registry array has invalid separators")


def _classify_registry_blob(manifest_bytes: bytes, blob: bytes) -> tuple[str, RegistryIdentity]:
    identity = _registry_identity_from_manifest(manifest_bytes)
    if _looks_like_lfs_pointer(blob):
        oid, size = _parse_exact_lfs_pointer(blob)
        if (oid, size) != (identity.sha256, identity.size):
            raise ValueError("LFS pointer identity does not match the committed manifest")
        return "lfs_pointer", identity
    if len(blob) != identity.size or sha256_bytes(blob) != identity.sha256:
        raise ValueError("materialized Registry bytes do not match the committed manifest")
    _validate_json_array_of_objects(blob)
    return "materialized", identity


def _select_payload_candidate(
    identity: RegistryIdentity,
    candidates: list[tuple[str, int, str]],
) -> str:
    for name, size, digest in candidates:
        if size == identity.size and digest == identity.sha256:
            return name
    raise ValueError("no local Registry payload matches the committed manifest")


class RegistryRepresentationClassificationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.payload = b'[{"dataset":"offline"}]\n'
        self.digest = sha256_bytes(self.payload)
        self.identity = RegistryIdentity(CANONICAL_REGISTRY_PATH, len(self.payload), self.digest)
        self.manifest = write_json_bytes({
            "source_registry": CANONICAL_REGISTRY_PATH,
            "artifacts": [{
                "path": CANONICAL_REGISTRY_PATH,
                "kind": "registry",
                "bytes": len(self.payload),
                "sha256": self.digest,
            }],
        })
        self.pointer = (
            f"{LFS_POINTER_VERSION}\n"
            f"oid sha256:{self.digest}\n"
            f"size {len(self.payload)}\n"
        ).encode("ascii")

    def test_manifest_anchors_pointer_and_equivalent_materialized_bytes(self) -> None:
        self.assertEqual(_classify_registry_blob(self.manifest, self.pointer), ("lfs_pointer", self.identity))
        self.assertEqual(_classify_registry_blob(self.manifest, self.payload), ("materialized", self.identity))
        url_payload = b'[{"description":"https://git-lfs.github.com/spec/v1"}]\n'
        url_manifest = write_json_bytes({
            "source_registry": CANONICAL_REGISTRY_PATH,
            "artifacts": [{
                "path": CANONICAL_REGISTRY_PATH,
                "kind": "registry",
                "bytes": len(url_payload),
                "sha256": sha256_bytes(url_payload),
            }],
        })
        self.assertEqual(
            _classify_registry_blob(url_manifest, url_payload)[0],
            "materialized",
        )

    def test_pointer_fields_must_be_exact_and_match_manifest(self) -> None:
        malformed = [
            self.pointer.replace(LFS_POINTER_VERSION.encode(), b"version https://git-lfs.github.com/spec/v9"),
            self.pointer + b"ext-1 abc\n",
            self.pointer.replace(f"oid sha256:{self.digest}".encode(), b"oid sha256:" + b"A" * 64),
            self.pointer.replace(f"oid sha256:{self.digest}".encode(), b"oid sha256:" + b"1" * 64),
            self.pointer.replace(f"size {len(self.payload)}".encode(), b"size 0"),
            self.pointer.replace(
                f"size {len(self.payload)}".encode(),
                f"size {len(self.payload) + 1}".encode(),
            ),
            self.pointer.replace(
                f"size {len(self.payload)}\n".encode(),
                f"size {len(self.payload)}\noid sha256:{self.digest}\n".encode(),
            ),
            self.pointer.replace(
                f"size {len(self.payload)}".encode(),
                f"size {len(self.payload)}\nextra ambiguous field\n".encode(),
            ),
        ]
        for candidate in malformed:
            with self.subTest(candidate=candidate[:100]):
                with self.assertRaises(ValueError):
                    _classify_registry_blob(self.manifest, candidate)

    def test_materialized_content_requires_exact_manifest_and_array_of_objects(self) -> None:
        for candidate in (
            b"not json\n",
            b'{"not":"an array"}\n',
            b'[1]\n',
            b'[null]\n',
            b'[{"duplicate":1,"duplicate":2}]\n',
            b'[{}] trailing\n',
        ):
            manifest = write_json_bytes({
                "source_registry": CANONICAL_REGISTRY_PATH,
                "artifacts": [{
                    "path": CANONICAL_REGISTRY_PATH,
                    "kind": "registry",
                    "bytes": len(candidate),
                    "sha256": sha256_bytes(candidate),
                }],
            })
            with self.subTest(candidate=candidate):
                with self.assertRaises(ValueError):
                    _classify_registry_blob(manifest, candidate)
        with self.assertRaisesRegex(ValueError, "do not match"):
            _classify_registry_blob(self.manifest, b'[{"dataset":"different"}]\n')

    def test_manifest_requires_one_well_formed_registry_identity(self) -> None:
        base = json.loads(self.manifest)
        cases = [
            {**base, "source_registry": "data/other.json"},
            {**base, "artifacts": []},
            {**base, "artifacts": [*base["artifacts"], base["artifacts"][0]]},
            {**base, "artifacts": [{**base["artifacts"][0], "bytes": True}]},
            {**base, "artifacts": [{**base["artifacts"][0], "bytes": 0}]},
            {**base, "artifacts": [{**base["artifacts"][0], "sha256": "A" * 64}]},
            {**base, "artifacts": [{**base["artifacts"][0], "kind": "other"}]},
        ]
        for value in cases:
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    _registry_identity_from_manifest(write_json_bytes(value))
        with self.assertRaises(ValueError):
            _registry_identity_from_manifest(b'{"source_registry":"x","source_registry":"y"}')

    def test_payload_selection_uses_exact_local_fallback_or_fails_closed(self) -> None:
        self.assertEqual(
            _select_payload_candidate(
                self.identity,
                [
                    ("working_tree", len(self.payload), "0" * 64),
                    ("local_lfs_object", len(self.payload), self.digest),
                ],
            ),
            "local_lfs_object",
        )
        with self.assertRaisesRegex(ValueError, "no local Registry payload"):
            _select_payload_candidate(
                self.identity,
                [("working_tree", len(self.payload), "0" * 64)],
            )


def write_json_bytes(value: object) -> bytes:
    return (json.dumps(value, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")


class ApplyRuntimeFreshnessImportIntegrationTest(unittest.TestCase):
    """Run the actual transaction and generators; adapt only the external CLI."""

    @classmethod
    def setUpClass(
        cls,
        *,
        materialize_registry: bool = True,
        source_revision: str | None = None,
        source_root: pathlib.Path | None = None,
    ) -> None:
        cls.temporary = tempfile.TemporaryDirectory(prefix="runtime-import-736-integration-")
        cls.temp_root = pathlib.Path(cls.temporary.name)
        cls.repo = cls.temp_root / "repo"
        cls.adapter = cls.temp_root / "datapan-test-adapter.py"
        cls.command_log = cls.temp_root / "external-cli.jsonl"
        cls.report = cls.temp_root / "sanitized-report.json"
        cls.receipt = cls.temp_root / "run-receipt.json"
        cls.run_id = "fixture-736-20261005"
        cls._create_offline_repo(
            materialize_registry=materialize_registry,
            source_revision=source_revision,
            source_root=source_root,
        )
        cls._create_external_cli_adapter()
        cls._create_sanitized_inputs()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.temporary.cleanup()

    @classmethod
    def _git(
        cls,
        *args: str,
        check: bool = True,
        env: dict[str, str] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        git_env = {**os.environ, "GIT_LFS_SKIP_SMUDGE": "1", **(env or {})}
        result = run(["git", *args], cwd=cls.repo, env=git_env)
        if check and result.returncode:
            raise AssertionError(f"git {' '.join(args)} failed: {result.stderr[-2000:]}")
        return result

    @classmethod
    def _create_ci_like_shallow_source(
        cls,
        *,
        source_revision: str | None = None,
        source_root: pathlib.Path | None = None,
    ) -> tuple[pathlib.Path, str, list[tuple[str, int]], str, str]:
        source_root = source_root or ROOT
        source_git = cls.temp_root / "ci-like-source.git"
        initialized = run(["git", "init", "--bare", str(source_git)], cwd=cls.temp_root)
        if initialized.returncode:
            raise AssertionError(f"local shallow source init failed: {initialized.stderr[-1000:]}")

        source_head = run(
            ["git", "rev-parse", "--verify", f"{source_revision}^{{commit}}"]
            if source_revision is not None else ["git", "rev-parse", "HEAD"],
            cwd=source_root,
        )
        if source_head.returncode:
            raise AssertionError(f"cannot resolve source fixture HEAD: {source_head.stderr[-1000:]}")
        source_revision = source_head.stdout.strip()
        # This is a synthetic local checkout baseline for the disposable
        # transaction fixture, not an origin/main trust or provenance claim.
        fetched_baseline = run(
            ["git", "--git-dir", str(source_git), "fetch", "--no-tags", "--depth=1",
             str(source_root), f"{source_revision}:refs/heads/ci-fixture-main-baseline"],
            cwd=cls.temp_root,
        )
        if fetched_baseline.returncode:
            raise AssertionError(f"local shallow synthetic baseline fetch failed: {fetched_baseline.stderr[-1000:]}")

        fetched_main = run(["git", "rev-parse", "--verify", "refs/remotes/origin/main"], cwd=source_root)
        if fetched_main.returncode:
            raise AssertionError(f"cannot resolve the actual fetched origin/main: {fetched_main.stderr[-1000:]}")
        main_revision = fetched_main.stdout.strip()

        historical = json.loads(
            (source_root / "tests/fixtures/diagnostic-source-applicability/health-probe-catalog.provenance.v1.json")
            .read_text(encoding="utf-8")
        )
        history_pins = [
            (historical["git_commit"], 1),
            ("e34062309a48b0e0b6c0f38add32f0cdec088616", 8),
            ("1a3088f64c0ff00fbf31e0e28cb37e3fc3d7dc07", 1),
        ]
        retained_attestation = json.loads(
            (source_root / "reports/runtime-freshness-import-attestations/35798122454.json")
            .read_text(encoding="utf-8")
        )
        retained_merge = retained_attestation.get("import", {}).get("merge_commit")
        if not isinstance(retained_merge, str) or len(retained_merge) != 40:
            raise AssertionError("retained historical import attestation lacks its exact merge commit")
        head_ref = run(
            ["git", "--git-dir", str(source_git), "symbolic-ref", "HEAD", "refs/heads/ci-fixture-main-baseline"],
            cwd=cls.temp_root,
        )
        if head_ref.returncode:
            raise AssertionError(f"local shallow source HEAD setup failed: {head_ref.stderr[-1000:]}")
        return source_git, source_revision, history_pins, main_revision, retained_merge

    @classmethod
    def _create_offline_repo(
        cls,
        *,
        materialize_registry: bool = True,
        source_revision: str | None = None,
        source_root: pathlib.Path | None = None,
    ) -> None:
        # Reproduce checkout depth and exact-history fetches from Verify Release
        # without network access. A shallow local clone can omit commits that
        # exist only in the parent's FETCH_HEAD.
        source_root = source_root or ROOT
        cls.source_root = source_root
        source_git, source_revision, history_pins, main_revision, retained_merge = cls._create_ci_like_shallow_source(
            source_revision=source_revision,
            source_root=source_root,
        )
        cls.source_revision = source_revision
        result = run(
            ["git", "clone", "--shared", "--no-checkout", "--quiet", str(source_git), str(cls.repo)],
            cwd=cls.temp_root,
            env={**os.environ, "GIT_LFS_SKIP_SMUDGE": "1"},
        )
        if result.returncode:
            raise AssertionError(f"local fixture clone failed: {result.stderr[-2000:]}")
        origin_main = run(["git", "show-ref", "--verify", "--quiet", "refs/remotes/origin/main"], cwd=cls.repo)
        if origin_main.returncode == 0:
            raise AssertionError("CI-like fixture unexpectedly contains origin/main")
        if origin_main.returncode != 1:
            raise AssertionError(f"cannot verify origin/main absence in CI-like fixture: {origin_main.stderr[-1000:]}")

        historical = json.loads(
            (source_root / "tests/fixtures/diagnostic-source-applicability/health-probe-catalog.provenance.v1.json")
            .read_text(encoding="utf-8")
        )
        historical_commit = historical["git_commit"]
        historical_path = "reports/health-probe-catalog.json"
        unavailable = run(["git", "rev-parse", f"{historical_commit}:{historical_path}"], cwd=cls.repo)
        if unavailable.returncode == 0:
            raise AssertionError("CI-like shallow clone unexpectedly inherited the FETCH_HEAD-only historical health pin")

        # Keep the initial shallow-clone assertions above independent from the
        # actual fetched main history. Add that authenticated commit only after
        # the exact historical pins are checked; otherwise those pins become
        # reachable from main and the FETCH_HEAD-only control stops testing its
        # intended boundary.
        for commit, depth in history_pins:
            fetched_pin = run(
                ["git", "--git-dir", str(source_git), "fetch", "--no-tags", f"--depth={depth}",
                 str(source_root), commit],
                cwd=cls.temp_root,
            )
            if fetched_pin.returncode:
                raise AssertionError(f"local shallow source pin fetch failed for {commit}: {fetched_pin.stderr[-1000:]}")

        copied_main = run(
            ["git", "--git-dir", str(source_git), "fetch", "--no-tags", "--depth=64",
             str(source_root), "refs/remotes/origin/main:refs/ci-fixture/fetched-origin-main"],
            cwd=cls.temp_root,
        )
        if copied_main.returncode:
            raise AssertionError(f"cannot copy the fetched origin/main into the hidden fixture ref: {copied_main.stderr[-1000:]}")
        copied_main_sha = run(
            ["git", "--git-dir", str(source_git), "rev-parse", "--verify", "refs/ci-fixture/fetched-origin-main"],
            cwd=cls.temp_root,
        )
        if copied_main_sha.returncode or copied_main_sha.stdout.strip() != main_revision:
            raise AssertionError("hidden fixture main ref differs from the actual fetched origin/main SHA")
        advertised_main = run(
            ["git", "--git-dir", str(source_git), "show-ref", "--verify", "--quiet", "refs/heads/main"],
            cwd=cls.temp_root,
        )
        if advertised_main.returncode == 0:
            raise AssertionError("synthetic source must not advertise a fixture-created refs/heads/main")
        if advertised_main.returncode != 1:
            raise AssertionError(f"cannot verify synthetic source main-branch absence: {advertised_main.stderr[-1000:]}")

        # Exact historical pin fetches may mark a commit on the trusted main
        # ancestry shallow. Deepen only the already-authenticated main ref so
        # those pins cannot hide its real parent chain from the checker.
        deepened_source_main = run(
            ["git", "--git-dir", str(source_git), "fetch", "--no-tags", "--deepen=64",
             str(source_root), "refs/remotes/origin/main:refs/ci-fixture/fetched-origin-main"],
            cwd=cls.temp_root,
        )
        if deepened_source_main.returncode:
            raise AssertionError(f"cannot deepen the exact fetched origin/main ancestry: {deepened_source_main.stderr[-1000:]}")
        source_has_retained_merge = run(
            ["git", "--git-dir", str(source_git), "merge-base", "--is-ancestor", retained_merge,
             "refs/ci-fixture/fetched-origin-main"],
            cwd=cls.temp_root,
        )
        if source_has_retained_merge.returncode:
            raise AssertionError("deepened fetched origin/main does not contain the retained import merge commit")

        # Transfer the explicitly fetched CI pins into this clone's own object
        # database so linked transaction worktrees can verify historical Git evidence.
        for commit, depth in history_pins:
            fetched_pin = run(
                ["git", "fetch", "--no-tags", f"--depth={depth}", str(source_git), commit],
                cwd=cls.repo,
            )
            if fetched_pin.returncode:
                raise AssertionError(f"fixture history-pin fetch failed for {commit}: {fetched_pin.stderr[-1000:]}")
        resolved_history_blob = cls._git("rev-parse", f"{historical_commit}:{historical_path}").stdout.strip()
        if resolved_history_blob != historical["git_blob"]:
            raise AssertionError("fixture history-pin fetch did not preserve the pinned historical health blob")

        fetched_main = run(
            ["git", "fetch", "--no-tags", "--depth=64", str(source_git),
             "refs/ci-fixture/fetched-origin-main:refs/remotes/origin/main"],
            cwd=cls.repo,
        )
        if fetched_main.returncode:
            raise AssertionError(f"fixture could not fetch the exact authenticated origin/main ref: {fetched_main.stderr[-1000:]}")
        resolved_main = cls._git("rev-parse", "--verify", "refs/remotes/origin/main").stdout.strip()
        if resolved_main != main_revision:
            raise AssertionError("CI-like fixture origin/main does not match the exact fetched source origin/main SHA")
        deepened_clone_main = run(
            ["git", "fetch", "--no-tags", "--deepen=64", str(source_git),
             "refs/ci-fixture/fetched-origin-main:refs/remotes/origin/main"],
            cwd=cls.repo,
        )
        if deepened_clone_main.returncode:
            raise AssertionError(f"fixture could not deepen exact origin/main ancestry: {deepened_clone_main.stderr[-1000:]}")
        resolved_main = cls._git("rev-parse", "--verify", "refs/remotes/origin/main").stdout.strip()
        if resolved_main != main_revision:
            raise AssertionError("deepening origin/main changed the exact fetched main SHA")
        clone_has_retained_merge = cls._git(
            "merge-base", "--is-ancestor", retained_merge, "refs/remotes/origin/main", check=False,
        )
        if clone_has_retained_merge.returncode:
            raise AssertionError("deepened fixture origin/main does not contain the retained import merge commit")

        checkout = run(
            ["git", "checkout", "--detach", "HEAD"], cwd=cls.repo,
            env={**os.environ, "GIT_LFS_SKIP_SMUDGE": "1"},
        )
        if checkout.returncode:
            raise AssertionError(f"local fixture checkout failed: {checkout.stderr[-2000:]}")
        if cls._git("rev-parse", "HEAD").stdout.strip() != source_revision:
            raise AssertionError("shallow fixture checkout differs from the exact source revision")

        source_manifest = subprocess.run(
            ["git", "show", f"{source_revision}:manifest.json"], cwd=source_root,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True,
        ).stdout
        committed_registry_blob = subprocess.run(
            ["git", "show", f"{source_revision}:{CANONICAL_REGISTRY_PATH}"], cwd=source_root,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True,
        ).stdout
        cls.original_registry_blob_sha256 = sha256_bytes(committed_registry_blob)
        cls.registry_representation, identity = _classify_registry_blob(source_manifest, committed_registry_blob)
        cls.registry_identity = identity
        if not materialize_registry:
            if cls.registry_representation != "lfs_pointer":
                raise AssertionError("pointer-preserving fixture requires a committed LFS pointer source")
            checked_out_blob = (cls.repo / identity.path).read_bytes()
            if checked_out_blob != committed_registry_blob:
                raise AssertionError("pointer-preserving fixture checkout changed the committed Registry pointer")
            cls.fixture_revision = source_revision
            if cls._git("status", "--porcelain").stdout.strip():
                raise AssertionError("pointer-preserving fixture baseline is not clean")
            return

        registry = cls.repo / identity.path
        if cls.registry_representation == "materialized":
            registry.write_bytes(committed_registry_blob)
        else:
            common = run(
                ["git", "rev-parse", "--path-format=absolute", "--git-common-dir"], cwd=source_root,
            )
            if common.returncode:
                raise AssertionError(f"cannot resolve source Git common directory: {common.stderr[-1000:]}")
            common_path = pathlib.Path(common.stdout.strip())
            source_registry = source_root / identity.path
            candidates: list[tuple[str, int, str]] = []
            candidate_paths: dict[str, pathlib.Path] = {}
            if source_registry.is_file() and source_registry.stat().st_size == identity.size:
                candidates.append(("working_tree", identity.size, sha256_file(source_registry)))
                candidate_paths["working_tree"] = source_registry
            lfs_object = common_path / "lfs/objects" / identity.sha256[:2] / identity.sha256[2:4] / identity.sha256
            if lfs_object.is_file() and lfs_object.stat().st_size == identity.size:
                candidates.append(("local_lfs_object", identity.size, sha256_file(lfs_object)))
                candidate_paths["local_lfs_object"] = lfs_object
            candidate_name = _select_payload_candidate(identity, candidates)
            shutil.copyfile(candidate_paths[candidate_name], registry)

        attributes = cls.repo / ".gitattributes"
        kept = [line for line in attributes.read_text(encoding="utf-8").splitlines()
                if not line.startswith(f"{identity.path} ")]
        attributes.write_text("\n".join(kept) + "\n", encoding="utf-8")
        policy_path = cls.repo / "policy/registry-distribution.json"
        policy = json.loads(policy_path.read_text(encoding="utf-8"))
        policy["canonical_registry"]["manifest_sha256"] = identity.sha256
        write_json(policy_path, policy)

        # Include the current caller under test if it is still locally modified.
        shutil.copyfile(SCRIPT, cls.repo / "scripts/apply-runtime-freshness-import.py")
        cls._git("config", "user.name", "Codex Runtime Import Fixture")
        cls._git("config", "user.email", "codex-runtime-fixture@example.invalid")
        cls._git("add", ".gitattributes", "data/data-go-kr.registry.json",
                 "policy/registry-distribution.json", "scripts/apply-runtime-freshness-import.py")
        staged_changes = cls._git("diff", "--cached", "--quiet", check=False)
        if staged_changes.returncode == 1:
            cls._git("commit", "-m", "Prepare local-only runtime import fixture")
        elif staged_changes.returncode != 0:
            raise AssertionError(f"cannot inspect staged fixture setup: {staged_changes.stderr[-1000:]}")
        cls.fixture_revision = cls._git("rev-parse", "HEAD").stdout.strip()

        materialized = run([sys.executable, "scripts/materialize-canonical-registry.py"], cwd=cls.repo)
        if materialized.returncode or '"status": "reused"' not in materialized.stdout:
            raise AssertionError(
                "real materializer did not reuse the local Registry payload; "
                f"stdout={materialized.stdout[-1000:]} stderr={materialized.stderr[-1000:]}"
            )
        if registry.stat().st_size != identity.size or sha256_file(registry) != identity.sha256:
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
        source_registry_blob = subprocess.run(
            ["git", "show", f"{self.source_revision}:{CANONICAL_REGISTRY_PATH}"], cwd=self.source_root,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True,
        ).stdout
        self.assertEqual(sha256_bytes(source_registry_blob), self.original_registry_blob_sha256)

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
