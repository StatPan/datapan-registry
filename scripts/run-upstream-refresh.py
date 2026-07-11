#!/usr/bin/env python3
"""Collect a candidate upstream snapshot and emit non-publishing drift evidence."""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import pathlib
import re
import subprocess
import sys
from typing import Any

try:
    import jsonschema
except ImportError as exc:
    raise SystemExit("missing dependency: install jsonschema") from exc


SECRET_PATTERNS = [
    re.compile(r"(?i)(serviceKey|api[_-]?key|authorization)(=|:)[^&\s]+"),
    re.compile(r"(?i)bearer\s+[^\s]+"),
]


def load(path: pathlib.Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def write(path: pathlib.Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def digest(path: pathlib.Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def asset(path: pathlib.Path) -> dict[str, Any]:
    value = load(path)
    if not isinstance(value, list):
        raise ValueError(f"registry must be an array: {path}")
    return {"path": path.as_posix(), "bytes": path.stat().st_size, "sha256": digest(path), "records": len(value)}


def redact(message: str) -> str:
    result = message.strip()[:2000]
    for pattern in SECRET_PATTERNS:
        result = pattern.sub(lambda match: f"{match.group(1) if match.lastindex else 'secret'}[REDACTED]", result)
    return result


def source_config(policy: dict[str, Any], source_id: str) -> dict[str, Any]:
    matches = [row for row in policy.get("sources", []) if row.get("source_id") == source_id]
    if len(matches) != 1:
        raise ValueError(f"refresh policy must contain exactly one {source_id}")
    return matches[0]


def run(command: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, text=True, capture_output=True, check=False)  # noqa: S603


def normalize_diff(path: pathlib.Path, observed_at: str, baseline: pathlib.Path, candidate: pathlib.Path) -> dict[str, Any]:
    report = load(path)
    if not isinstance(report, dict):
        raise ValueError("catalog diff must be an object")
    report["generated_at"] = observed_at
    report["old"] = baseline.as_posix()
    report["new"] = candidate.as_posix()
    for key in ("added", "removed", "changed"):
        if isinstance(report.get(key), list):
            report[key] = sorted(report[key], key=lambda row: (str(row.get("id", "")), json.dumps(row, sort_keys=True)))
    write(path, report)
    return report


def work_key(source_id: str, status: str, summary: dict[str, Any] | None, error_class: str | None) -> str:
    identity = json.dumps({"source_id": source_id, "status": status, "summary": summary, "error_class": error_class}, sort_keys=True)
    return f"upstream-refresh:{source_id}:{hashlib.sha256(identity.encode()).hexdigest()[:16]}"


def evidence_base(config: dict[str, Any], observed_at: str, baseline_asset: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema_version": "datapan.upstream-refresh-evidence.v1",
        "observed_at": observed_at,
        "source_id": config["source_id"],
        "owner": config["owner"],
        "baseline": baseline_asset,
        "publication": {
            "automatic": False,
            "release_allowed": False,
            "required_gates": config["publication"]["required_gates"],
        },
    }


def write_outputs(output_dir: pathlib.Path, evidence: dict[str, Any], schema: pathlib.Path) -> None:
    evidence_path = output_dir / "upstream-refresh-evidence.json"
    write(evidence_path, evidence)
    jsonschema.Draft202012Validator(load(schema)).validate(evidence)
    review = evidence["review"]
    packet = {
        "schema_version": "datapan.upstream-refresh-work-packet.v1",
        "source_id": evidence["source_id"],
        "observed_at": evidence["observed_at"],
        "status": evidence["status"],
        "owner": evidence["owner"],
        "work_key": review["work_key"],
        "action": review["action"],
        "evidence": evidence_path.as_posix(),
        "snapshot": evidence["snapshot"]["path"] if evidence["snapshot"] else None,
        "diff": evidence["diff"]["path"] if evidence["diff"] else None,
        "automatic_publication": False,
    }
    write(output_dir / "upstream-refresh-work-packet.json", packet)
    packet_schema = pathlib.Path("schemas/datapan.upstream-refresh-work-packet.v1.schema.json")
    jsonschema.Draft202012Validator(load(packet_schema)).validate(packet)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", default="data_go_kr")
    parser.add_argument("--policy", type=pathlib.Path, default=pathlib.Path("policy/source-refresh.json"))
    parser.add_argument("--schema", type=pathlib.Path, default=pathlib.Path("schemas/datapan.upstream-refresh-evidence.v1.schema.json"))
    parser.add_argument("--datapan", default="datapan")
    parser.add_argument("--output-dir", type=pathlib.Path, default=pathlib.Path(".datapan/ci/upstream-refresh"))
    parser.add_argument("--observed-at")
    args = parser.parse_args()
    observed_at = args.observed_at or dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    evidence_path = args.output_dir / "upstream-refresh-evidence.json"
    try:
        policy = load(args.policy)
        config = source_config(policy, args.source)
        baseline = pathlib.Path(config["canonical_registry"])
        baseline_asset = asset(baseline)
        candidate = args.output_dir / "candidate.registry.json"
        diff_path = args.output_dir / "catalog-diff.json"
        arguments = [str(value).replace("{candidate_registry}", candidate.as_posix()) for value in config["importer"]["arguments"]]
        collection = run([args.datapan, *arguments])
        if collection.returncode != 0:
            parsed: dict[str, Any] = {}
            try:
                candidate_error = json.loads(collection.stdout)
                if isinstance(candidate_error, dict):
                    parsed = candidate_error
            except json.JSONDecodeError:
                pass
            error_class = str(parsed.get("error") or ("missing_credential" if not os.environ.get(config["credential_env"]) else "collection_command_failed"))
            message = redact(str(parsed.get("message") or collection.stderr or collection.stdout or error_class))
            evidence = evidence_base(config, observed_at, baseline_asset) | {
                "status": "collection_failure",
                "collection": {"attempted": True, "succeeded": False, "exit_code": collection.returncode, "error_class": error_class, "message": message},
                "snapshot": None,
                "diff": None,
                "review": {"action": "investigate_collection_failure", "work_key": work_key(args.source, "collection_failure", None, error_class)},
            }
            write_outputs(args.output_dir, evidence, args.schema)
            print(json.dumps({"status": "collection_failure", "evidence": evidence_path.as_posix(), "work_key": evidence["review"]["work_key"]}))
            return 2
        if not candidate.exists():
            raise ValueError("importer succeeded without writing candidate registry")
        diff_result = run([args.datapan, "catalog", "diff", "--old", baseline.as_posix(), "--new", candidate.as_posix(), "--limit", "0", "--output", diff_path.as_posix(), "--json"])
        if diff_result.returncode != 0:
            raise ValueError(f"catalog diff failed: {redact(diff_result.stderr or diff_result.stdout)}")
        diff = normalize_diff(diff_path, observed_at, baseline, candidate)
        jsonschema.Draft202012Validator(load(pathlib.Path("schemas/datapan.catalog-diff.v1.schema.json"))).validate(diff)
        summary = diff["summary"]
        material = any(int(summary[field]) > 0 for field in config["diff"]["material_change_fields"])
        status = "material_change" if material else "no_change"
        evidence = evidence_base(config, observed_at, baseline_asset) | {
            "status": status,
            "collection": {"attempted": True, "succeeded": True, "exit_code": 0, "error_class": None},
            "snapshot": asset(candidate),
            "diff": {"path": diff_path.as_posix(), "sha256": digest(diff_path), "summary": summary},
            "review": {"action": "review_catalog_drift" if material else "none", "work_key": work_key(args.source, status, summary, None)},
        }
        write_outputs(args.output_dir, evidence, args.schema)
        print(json.dumps({"status": status, "evidence": evidence_path.as_posix(), "work_key": evidence["review"]["work_key"]}))
        return 0
    except Exception as exc:  # noqa: BLE001
        print(f"FAIL upstream refresh: {redact(str(exc))}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
