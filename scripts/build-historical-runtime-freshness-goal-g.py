from __future__ import annotations

import collections
import hashlib
import json
import pathlib
import subprocess
import sys

REPO = pathlib.Path(sys.argv[1]).resolve()
ARCHIVE = REPO / "historical-evidence" / "runtime-freshness-goal-g"
MAIN_BASE = "151da89679e34e3d7c034077c3a45a7c648b9de5"
MERGE_649 = "15fbb113e23c146fc61337b251077ced38b82375"
SPECS = [
    (642, "35934661938", "44aaf067056c6ba231685ca9284a3cfbcd861ca5", "81fe0c21e83eee6f0a5a7704ca47f6f4a44d73a5", "automation/runtime-freshness-35934661938"),
    (643, "36074831711", "44aaf067056c6ba231685ca9284a3cfbcd861ca5", "96c2ca037e7a949a9e8fc0e251ee46368bd857d4", "automation/runtime-freshness-36074831711"),
    (644, "36202899918", "44aaf067056c6ba231685ca9284a3cfbcd861ca5", "8f84449569d257049e7bcd3999fb999e944c85b6", "automation/runtime-freshness-36202899918"),
    (645, "36279928741", "44aaf067056c6ba231685ca9284a3cfbcd861ca5", "322d09e89324a88dffe943d355037bba740d57cf", "automation/runtime-freshness-36279928741"),
    (648, "36505604187", "4321df1868754045ff3705b5c133c9fad2abde6e", "3dddb3db2ee3a7256977f024e1f0e631bda37497", "automation/runtime-freshness-36505604187"),
    (650, "36650152644", "15fbb113e23c146fc61337b251077ced38b82375", "2032512c475c095c57566d63c5c87e74dbcdf1a6", "automation/runtime-freshness-attestation-36650152644"),
    (654, "36796641853", "15fbb113e23c146fc61337b251077ced38b82375", "32fa26dc24676c41f23ae3b6485dbe178f026201", "automation/runtime-freshness-36796641853"),
    (671, "36947534151", "15fbb113e23c146fc61337b251077ced38b82375", "1f0796fe97048a1d74d798d947a06a206e9f86ef", "automation/runtime-freshness-36947534151"),
    (672, "37082117038", "8ad86cb2d9adb311173d980e5964493778c57e4b", "97b53e7b706760f6261372953ee1c920084f3a48", "automation/runtime-freshness-37082117038"),
    (747, "37400712714", "6e871b6a7fbd9ae466d87792c6813d26f44e2eea", "27027b27d82aa812d90fd4a0a2e10006a18b96ed", "automation/runtime-freshness-37400712714"),
]
IMPORT_PRS = [x for x in SPECS if x[0] != 650]

def git_bytes(rev: str, path: str) -> bytes:
    return subprocess.check_output(["git", "-C", str(REPO), "show", f"{rev}:{path}"])

def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()

def parse(data: bytes):
    return json.loads(data.decode("utf-8"))

def canon(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))

def counter(rows):
    return collections.Counter(canon(row) for row in rows)

def dump(path: pathlib.Path, value) -> tuple[int, str]:
    data = (json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return len(data), sha(data)

def count_status(rows):
    result = {"verified": 0, "failed": 0, "skipped": 0, "unknown": 0}
    for row in rows:
        status = row.get("status")
        result[status if status in result else "unknown"] += 1
    return result

def commit_parent(rev: str) -> str:
    line = subprocess.check_output(["git", "-C", str(REPO), "rev-list", "--parents", "-n", "1", rev], text=True).strip().split()
    return line[1]

def main():
    if ARCHIVE.exists():
        raise SystemExit(f"archive already exists: {ARCHIVE}")
    ARCHIVE.mkdir(parents=True)
    records = []
    originals = []
    verification_total = 0
    failure_total = 0
    unique_absent_total = 0
    failure_by_pr = {}
    verification_by_pr = {}
    main_verification = parse(git_bytes(MAIN_BASE, "reports/latest-verification.json"))["results"]
    main_keys = set(counter(main_verification))
    protected = ["manifest.json"]
    tracked_reports = subprocess.check_output(["git", "-C", str(REPO), "ls-tree", "-r", "--name-only", "HEAD", "--", "reports"], text=True).splitlines()
    protected.extend(sorted(tracked_reports))
    protected_inventory = []
    for path in protected:
        data = git_bytes("HEAD", path)
        protected_inventory.append({"path": path, "bytes": len(data), "sha256": sha(data)})

    for number, run_id, parent, head, head_ref in SPECS:
        head_admission_path = f"reports/runtime-freshness-import-admissions/{run_id}.json"
        head_receipt_path = f"reports/runtime-freshness-imports/{run_id}.json"
        pr = {
            "number": number,
            "run_id": run_id,
            "head_ref": head_ref,
            "parent": parent,
            "head": head,
            "first_parent_verified": commit_parent(head) == parent,
        }
        if number == 650:
            source_path = "reports/runtime-freshness-import-attestations/36650152644.json"
            data = git_bytes(head, source_path)
            archive_path = f"originals/pr-{number}/attestation-{run_id}.json"
            (ARCHIVE / archive_path).parent.mkdir(parents=True, exist_ok=True)
            (ARCHIVE / archive_path).write_bytes(data)
            originals.append({"kind": "attestation", "pr": number, "run_id": run_id, "source_commit": head, "source_path": source_path, "archive_path": archive_path, "bytes": len(data), "sha256": sha(data)})
            pr["attestation"] = archive_path
            pr["attestation_sha256"] = sha(data)
            records.append(pr)
            continue

        for kind, source_path, prefix in [
            ("admission", head_admission_path, "admission"),
            ("receipt", head_receipt_path, "receipt"),
        ]:
            data = git_bytes(head, source_path)
            archive_path = f"originals/pr-{number}/{prefix}-{run_id}.json"
            (ARCHIVE / archive_path).parent.mkdir(parents=True, exist_ok=True)
            (ARCHIVE / archive_path).write_bytes(data)
            originals.append({"kind": kind, "pr": number, "run_id": run_id, "source_commit": head, "source_path": source_path, "archive_path": archive_path, "bytes": len(data), "sha256": sha(data)})
            pr[f"{kind}_path"] = archive_path
            pr[f"{kind}_sha256"] = sha(data)

        for kind, source_path, key, prefix in [
            ("verification", "reports/latest-verification.json", "results", "verification"),
            ("failure", "reports/failure-observations.json", "observations", "failure"),
        ]:
            parent_data = git_bytes(parent, source_path)
            head_data = git_bytes(head, source_path)
            parent_json = parse(parent_data)
            head_json = parse(head_data)
            parent_rows = parent_json[key]
            head_rows = head_json[key]
            if head_rows[: len(parent_rows)] != parent_rows:
                raise SystemExit(f"not append-only: PR {number} {source_path}")
            delta = head_rows[len(parent_rows):]
            delta_counter = counter(delta)
            parent_counter = counter(parent_rows)
            main_counter = set(counter(main_verification)) if kind == "verification" else set()
            present_main = sum(1 for row in delta if canon(row) in main_counter) if kind == "verification" else 0
            absent_main = len(delta) - present_main if kind == "verification" else None
            unique_absent = len({canon(row) for row in delta if canon(row) not in main_keys}) if kind == "verification" else None
            identical_parent = sum(1 for row in delta if parent_counter[canon(row)] > 0)
            archive_path = f"deltas/pr-{number}/{prefix}.json"
            obj = {
                "schema_version": "datapan.historical-runtime-freshness-delta.v1",
                "kind": kind,
                "pr": number,
                "run_id": run_id,
                "source": {"parent": parent, "head": head, "path": source_path, "parent_sha256": sha(parent_data), "head_sha256": sha(head_data)},
                "parent_row_count": len(parent_rows),
                "head_row_count": len(head_rows),
                "delta_row_count": len(delta),
                "delta_distinct_row_count": len(delta_counter),
                "delta_rows_present_main": present_main,
                "delta_rows_absent_main": absent_main,
                "delta_unique_rows_absent_main": unique_absent,
                "delta_rows_identical_to_preexisting_parent": identical_parent,
                "rows": delta,
            }
            size, digest = dump(ARCHIVE / archive_path, obj)
            pr[f"{prefix}_delta_path"] = archive_path
            pr[f"{prefix}_delta_sha256"] = digest
            pr[f"{prefix}_source_parent_sha256"] = sha(parent_data)
            pr[f"{prefix}_source_head_sha256"] = sha(head_data)
            pr[f"{prefix}_delta_row_count"] = len(delta)
            pr[f"{prefix}_delta_distinct_row_count"] = len(delta_counter)
            pr[f"{prefix}_delta_rows_identical_to_preexisting_parent"] = identical_parent
            if kind == "verification":
                pr["verification_delta_rows_present_main"] = present_main
                pr["verification_delta_rows_absent_main"] = absent_main
                pr["verification_unique_rows_absent_main"] = unique_absent
                verification_total += len(delta)
                unique_absent_total += unique_absent
                verification_by_pr[number] = obj
            else:
                failure_total += len(delta)
                failure_by_pr[number] = obj
        records.append(pr)

    inventory = {
        "schema_version": "datapan.historical-runtime-freshness-goal-g.v1",
        "purpose": "Historical evidence preservation only; not current freshness, import-complete, release-approved, manifest, queue, or admission state.",
        "automatic_consumption": False,
        "active_claims": {"current_freshness": False, "import_complete": False, "release_approved": False, "admission_state": False},
        "offline_only": True,
        "source_audit_json_is_external_and_unchanged": True,
        "main_baseline": MAIN_BASE,
        "pr_649_merge": MERGE_649,
        "parent_to_head_verification_rows": verification_total,
        "unique_verification_rows_absent_from_main": unique_absent_total,
        "failure_observation_rows": failure_total,
        "pr_747_duplicate_rows_retained": verification_by_pr[747]["delta_rows_identical_to_preexisting_parent"],
        "original_file_counts": {"admissions": 9, "receipts": 9, "attestations": 1},
        "pr_records": records,
        "original_files": originals,
        "protected_active_paths": protected_inventory,
        "notes": [
            "The 6548 value is the total parent-to-head verification-row multiset delta, not the 5373 unique rows absent from main.",
            "PR #747 retains all 800 delta rows, including 346 rows identical to pre-existing parent rows.",
            "PR #650 attestation bindings are checked against the files and manifest/release ledger at merge commit #649; those files are not duplicated in the 9+9 original-file set.",
            "No active report, manifest, release ledger, queue, or admission-state path is part of this archive.",
        ],
    }
    dump(ARCHIVE / "inventory.json", inventory)
    print(json.dumps({"archive": str(ARCHIVE), "verification_rows": verification_total, "unique_absent": unique_absent_total, "failure_rows": failure_total, "originals": len(originals)}, indent=2))

if __name__ == "__main__":
    main()
