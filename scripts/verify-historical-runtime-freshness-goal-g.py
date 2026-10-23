from __future__ import annotations

import argparse
import collections
import hashlib
import json
import pathlib
import subprocess
import sys
from typing import Any

MAIN_BASE = "151da89679e34e3d7c034077c3a45a7c648b9de5"
MERGE_649 = "15fbb113e23c146fc61337b251077ced38b82375"
SOURCE_AUDIT = {
    "path": "/tmp/claude-1000/-home-statpan-workspace-datapan-data/f2c8d7af-fe46-41f9-b184-1b089acc9def/scratchpad/runtime-audit/audit-data.json",
    "sha256": "d793e1b412fb2157b502496d87034c831598ed69bcaa95a67a850c4bebdbe0eb",
    "bytes": 623997,
    "unchanged": True,
}
SPECS = {
    642: ("35934661938", "44aaf067056c6ba231685ca9284a3cfbcd861ca5", "81fe0c21e83eee6f0a5a7704ca47f6f4a44d73a5"),
    643: ("36074831711", "44aaf067056c6ba231685ca9284a3cfbcd861ca5", "96c2ca037e7a949a9e8fc0e251ee46368bd857d4"),
    644: ("36202899918", "44aaf067056c6ba231685ca9284a3cfbcd861ca5", "8f84449569d257049e7bcd3999fb999e944c85b6"),
    645: ("36279928741", "44aaf067056c6ba231685ca9284a3cfbcd861ca5", "322d09e89324a88dffe943d355037bba740d57cf"),
    648: ("36505604187", "4321df1868754045ff3705b5c133c9fad2abde6e", "3dddb3db2ee3a7256977f024e1f0e631bda37497"),
    650: ("36650152644", "15fbb113e23c146fc61337b251077ced38b82375", "2032512c475c095c57566d63c5c87e74dbcdf1a6"),
    654: ("36796641853", "15fbb113e23c146fc61337b251077ced38b82375", "32fa26dc24676c41f23ae3b6485dbe178f026201"),
    671: ("36947534151", "15fbb113e23c146fc61337b251077ced38b82375", "1f0796fe97048a1d74d798d947a06a206e9f86ef"),
    672: ("37082117038", "8ad86cb2d9adb311173d980e5964493778c57e4b", "97b53e7b706760f6261372953ee1c920084f3a48"),
    747: ("37400712714", "6e871b6a7fbd9ae466d87792c6813d26f44e2eea", "27027b27d82aa812d90fd4a0a2e10006a18b96ed"),
}

def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()

def canon(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))

def counter(rows: list[Any]) -> collections.Counter[str]:
    return collections.Counter(canon(row) for row in rows)

def git_bytes(repo: pathlib.Path, rev: str, path: str) -> bytes:
    return subprocess.check_output(["git", "-C", str(repo), "show", f"{rev}:{path}"])

def load_json(data: bytes, label: str) -> Any:
    try:
        return json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise AssertionError(f"{label}: invalid JSON: {exc}") from exc

def fail(message: str) -> None:
    raise AssertionError(message)

def receipt_counts(receipt: dict[str, Any]) -> dict[str, int]:
    results = receipt.get("results")
    if not isinstance(results, list):
        fail("receipt results are missing")
    statuses = {"verified": 0, "failed": 0, "skipped": 0, "unknown": 0}
    dispositions = {
        "classified_failure": 0,
        "unclassified_failure": 0,
        "healthy_recovery": 0,
        "no_active_failure": 0,
    }
    identities: set[str] = set()
    for index, row in enumerate(results):
        if not isinstance(row, dict):
            fail(f"receipt result {index} is not an object")
        subject_id = row.get("subject_id")
        if not isinstance(subject_id, str) or not subject_id or subject_id in identities:
            fail(f"receipt result {index} has a missing or duplicate subject_id")
        identities.add(subject_id)
        status = row.get("status")
        if status not in statuses:
            fail(f"receipt result {index} has an invalid status")
        statuses[status] += 1
        disposition = row.get("disposition")
        if disposition not in dispositions:
            fail(f"receipt result {index} has an invalid disposition")
        dispositions[disposition] += 1
    summary = receipt.get("summary")
    if not isinstance(summary, dict):
        fail("receipt summary is missing")
    expected = {
        "reported_results": len(results),
        "classified_failures": dispositions["classified_failure"],
        "unclassified_failures": dispositions["unclassified_failure"],
        "healthy_recoveries": dispositions["healthy_recovery"],
        "observations_projected": dispositions["classified_failure"],
    }
    if any(summary.get(key) != value for key, value in expected.items()):
        fail(f"receipt summary does not reconcile: expected={expected}, actual={summary}")
    return {"total": len(results), **statuses}

def verify_admission_receipt_linkage(
    *, admission: dict[str, Any], receipt: dict[str, Any], receipt_sha256: str, run_id: str, parent: str,
) -> None:
    if admission.get("run_id") != run_id or receipt.get("run_id") != run_id:
        fail(f"PR {run_id}: admission/receipt run ID mismatch")
    if admission.get("producer", {}).get("revision") != parent:
        fail(f"PR {run_id}: admission producer revision does not equal parent")
    receipt_inputs = receipt.get("inputs")
    admission_inputs = admission.get("inputs")
    if not isinstance(receipt_inputs, dict) or not isinstance(admission_inputs, dict):
        fail(f"PR {run_id}: receipt/admission inputs are missing")
    if receipt_inputs.get("sanitized_report_sha256") != admission_inputs.get("sanitized_report_sha256"):
        fail(f"PR {run_id}: sanitized report digest is not bound")
    if receipt_inputs.get("run_receipt_sha256") != admission_inputs.get("run_receipt_sha256"):
        fail(f"PR {run_id}: producer run receipt digest is not bound")
    selected = admission.get("selected_identity_set")
    planned = admission_inputs.get("planned_identity_set")
    if not isinstance(selected, dict) or not isinstance(planned, dict):
        fail(f"PR {run_id}: admission selected/planned identity sets are missing")
    receipt_result_counts = receipt_counts(receipt)
    if planned.get("count") != receipt_result_counts["total"]:
        fail(f"PR {run_id}: receipt result count does not equal planned identity count")
    selected_total = admission.get("arithmetic", {}).get("selected_new_results")
    if not isinstance(selected_total, int) or selected_total < 0 or selected_total > receipt_result_counts["total"]:
        fail(f"PR {run_id}: selected_new_results is outside receipt result count")
    if selected.get("count") != selected_total:
        fail(f"PR {run_id}: selected identity count does not equal selected_new_results")
    arithmetic = admission.get("arithmetic")
    if not isinstance(arithmetic, dict):
        fail(f"PR {run_id}: admission arithmetic is missing")
    selected_arithmetic = arithmetic.get("selected")
    if not isinstance(selected_arithmetic, dict):
        fail(f"PR {run_id}: selected arithmetic is missing")
    if selected_arithmetic.get("total") != selected_total:
        fail(f"PR {run_id}: selected arithmetic total does not match selected_new_results")
    if selected_arithmetic.get("total") != sum(selected_arithmetic.get(key, -1) for key in ("verified", "failed", "skipped", "unknown")):
        fail(f"PR {run_id}: selected arithmetic status counts do not sum to total")
    before = arithmetic.get("before")
    after = arithmetic.get("after")
    if not isinstance(before, dict) or not isinstance(after, dict):
        fail(f"PR {run_id}: before/after arithmetic is missing")
    if after.get("total") != before.get("total") + selected_total:
        fail(f"PR {run_id}: total arithmetic does not reconcile")
    for key in ("verified", "failed", "skipped", "unknown"):
        if after.get(key) != before.get(key) + selected_arithmetic.get(key):
            fail(f"PR {run_id}: {key} arithmetic does not reconcile")
    if admission_inputs.get("import_receipt", {}).get("sha256") != receipt_sha256:
        fail(f"PR {run_id}: admission receipt digest does not match original receipt bytes")

def verify(repo: pathlib.Path, archive: pathlib.Path) -> dict[str, Any]:
    inv_path = archive / "inventory.json"
    inventory = load_json(inv_path.read_bytes(), str(inv_path))
    if inventory.get("automatic_consumption") is not False:
        fail("archive must declare automatic_consumption=false")
    if inventory.get("active_claims") != {"admission_state": False, "current_freshness": False, "import_complete": False, "release_approved": False}:
        fail("archive active claim boundary changed")
    if inventory.get("main_baseline") != MAIN_BASE:
        fail("main baseline mismatch")
    if inventory.get("pr_649_merge") != MERGE_649:
        fail("#649 merge mismatch")
    if inventory.get("source_audit") != SOURCE_AUDIT:
        fail("supplied audit source digest/size/unchanged marker mismatch")
    consumer_scan = inventory.get("consumer_scan")
    if not isinstance(consumer_scan, dict) or consumer_scan.get("automatic_consumers") != []:
        fail("archive consumer scan does not prove zero automatic consumers")
    if inventory.get("original_file_counts") != {"admissions": 9, "attestations": 1, "receipts": 9}:
        fail("original file count mismatch")
    records = {item["number"]: item for item in inventory.get("pr_records", [])}
    if set(records) != set(SPECS):
        fail("PR record set mismatch")
    originals = inventory.get("original_files")
    if not isinstance(originals, list) or len(originals) != 19:
        fail("original inventory count mismatch")
    original_by_key = {(item["kind"], item["pr"]): item for item in originals}
    if len(original_by_key) != 19:
        fail("original inventory has duplicate kind/PR identity")

    main_verification = load_json(git_bytes(repo, MAIN_BASE, "reports/latest-verification.json"), "main verification")["results"]
    main_counter = counter(main_verification)
    total_verification = total_unique_absent = total_failures = 0
    for number, (run_id, parent, head) in SPECS.items():
        record = records[number]
        if record.get("parent") != parent or record.get("head") != head or record.get("run_id") != run_id:
            fail(f"PR {number}: fixed SHA/ID record mismatch")
        try:
            parent_line = subprocess.check_output(["git", "-C", str(repo), "rev-list", "--parents", "-n", "1", head], text=True).strip().split()
        except subprocess.CalledProcessError as exc:
            fail(f"PR {number}: source head unavailable offline: {exc}")
        if len(parent_line) < 2 or parent_line[1] != parent:
            fail(f"PR {number}: git first parent mismatch")
        if record.get("first_parent_verified") is not True:
            fail(f"PR {number}: inventory first-parent proof is false")
        for kind in ("admission", "receipt") if number != 650 else ():
            item = original_by_key[(kind, number)]
            expected_source_path = (
                f"reports/runtime-freshness-import-admissions/{run_id}.json"
                if kind == "admission"
                else f"reports/runtime-freshness-imports/{run_id}.json"
            )
            if item.get("source_commit") != head:
                fail(f"PR {number} {kind}: source commit is not the fixed PR head")
            if item.get("source_path") != expected_source_path:
                fail(f"PR {number} {kind}: source path is not the fixed evidence path")
            raw = git_bytes(repo, head, expected_source_path)
            local = (archive / item["archive_path"]).read_bytes()
            if raw != local:
                fail(f"PR {number} {kind}: archive bytes differ from fixed source head")
            if sha(local) != item["sha256"]:
                fail(f"PR {number} {kind}: original digest mismatch")
            if item.get("bytes") != len(local):
                fail(f"PR {number} {kind}: original byte count mismatch")
            data = load_json(local, item["archive_path"])
            if data.get("run_id") != run_id:
                fail(f"PR {number} {kind}: run ID mismatch")
            if kind == "admission":
                if data.get("producer", {}).get("revision") != parent:
                    fail(f"PR {number}: admission producer revision does not equal parent")
                ref = data.get("inputs", {}).get("import_receipt", {})
                receipt_item = original_by_key[("receipt", number)]
                if ref.get("path") != receipt_item["source_path"] or ref.get("sha256") != receipt_item["sha256"]:
                    fail(f"PR {number}: admission receipt binding mismatch")
                receipt_local = (archive / receipt_item["archive_path"]).read_bytes()
                receipt = load_json(receipt_local, receipt_item["archive_path"])
                verify_admission_receipt_linkage(
                    admission=data,
                    receipt=receipt,
                    receipt_sha256=sha(receipt_local),
                    run_id=run_id,
                    parent=parent,
                )
        if number == 650:
            item = original_by_key[("attestation", number)]
            expected_source_path = f"reports/runtime-freshness-import-attestations/{run_id}.json"
            if item.get("source_commit") != head:
                fail("PR 650 attestation: source commit is not the fixed PR head")
            if item.get("source_path") != expected_source_path:
                fail("PR 650 attestation: source path is not the fixed evidence path")
            raw = git_bytes(repo, head, expected_source_path)
            local = (archive / item["archive_path"]).read_bytes()
            if raw != local or sha(local) != item["sha256"] or item.get("bytes") != len(local):
                fail("PR 650: attestation bytes/digest mismatch")
            attestation = load_json(local, item["archive_path"])
            if attestation.get("import", {}).get("merge_commit") != MERGE_649:
                fail("PR 650: attestation is not bound to #649 merge")
            bindings = attestation.get("lineage", {})
            for name in ("admission", "import_receipt"):
                if not isinstance(bindings.get(name), dict) or len(bindings[name].get("sha256", "")) != 64:
                    fail(f"PR 650: missing {name} digest")
            digest_paths = {
                "admission": attestation["lineage"]["admission"]["path"],
                "import_receipt": attestation["lineage"]["import_receipt"]["path"],
                "manifest": attestation["registry"]["manifest"]["path"],
                "release_ledger": attestation["registry"]["release_ledger"]["path"],
            }
            digest_refs = {
                "admission": attestation["lineage"]["admission"],
                "import_receipt": attestation["lineage"]["import_receipt"],
                "manifest": attestation["registry"]["manifest"],
                "release_ledger": attestation["registry"]["release_ledger"],
            }
            for name, path in digest_paths.items():
                source = git_bytes(repo, MERGE_649, path)
                if sha(source) != digest_refs[name].get("sha256"):
                    fail(f"PR 650: {name} digest does not match #649 merge bytes")
            continue
        for kind, source_path, key, archive_key in (("verification", "reports/latest-verification.json", "results", "verification_delta_path"), ("failure", "reports/failure-observations.json", "observations", "failure_delta_path")):
            expected_source_path = source_path
            delta_path = record[archive_key]
            delta_obj = load_json((archive / delta_path).read_bytes(), delta_path)
            source_meta = delta_obj.get("source")
            if not isinstance(source_meta, dict):
                fail(f"PR {number} {kind}: source metadata is missing")
            if source_meta.get("parent") != parent or source_meta.get("head") != head or source_meta.get("path") != expected_source_path:
                fail(f"PR {number} {kind}: source identity/path is not the fixed PR comparison")
            parent_obj = load_json(git_bytes(repo, parent, expected_source_path), f"PR {number} parent {expected_source_path}")
            head_obj = load_json(git_bytes(repo, head, expected_source_path), f"PR {number} head {expected_source_path}")
            parent_rows, head_rows = parent_obj[key], head_obj[key]
            if head_rows[:len(parent_rows)] != parent_rows:
                fail(f"PR {number} {kind}: source is not append-only")
            expected = head_rows[len(parent_rows):]
            rows = delta_obj.get("rows")
            if counter(rows) != counter(expected):
                fail(f"PR {number} {kind}: archive multiset differs from fixed source delta")
            if delta_obj.get("delta_row_count") != len(expected) or delta_obj.get("delta_distinct_row_count") != len(counter(expected)):
                fail(f"PR {number} {kind}: archive counts mismatch")
            delta_bytes = (archive / record[archive_key]).read_bytes()
            if sha(delta_bytes) != record[f"{kind}_delta_sha256"]:
                fail(f"PR {number} {kind}: delta digest mismatch")
            if delta_obj.get("source", {}).get("parent_sha256") != sha(git_bytes(repo, parent, expected_source_path)) or delta_obj.get("source", {}).get("head_sha256") != sha(git_bytes(repo, head, expected_source_path)):
                fail(f"PR {number} {kind}: source blob digest mismatch")
            if kind == "verification":
                present = sum(1 for row in expected if canon(row) in main_counter)
                absent_unique = len({canon(row) for row in expected if canon(row) not in main_counter})
                if present != record["verification_delta_rows_present_main"] or absent_unique != record["verification_unique_rows_absent_main"]:
                    fail(f"PR {number} verification main comparison mismatch")
                total_verification += len(expected)
                total_unique_absent += absent_unique
            else:
                total_failures += len(expected)
    if total_verification != 6548 or total_unique_absent != 5373 or total_failures != 3570:
        fail(f"totals mismatch: verification={total_verification}, unique_absent={total_unique_absent}, failures={total_failures}")
    if records[747].get("verification_delta_rows_identical_to_preexisting_parent") != 346:
        fail("PR 747 duplicate retention count mismatch")
    if inventory.get("parent_to_head_verification_rows") != total_verification or inventory.get("unique_verification_rows_absent_from_main") != total_unique_absent or inventory.get("failure_observation_rows") != total_failures:
        fail("inventory totals mismatch")
    if inventory.get("pr_747_duplicate_rows_retained") != 346:
        fail("inventory #747 duplicate count mismatch")
    return {"status": "ok", "prs": 10, "originals": 19, "verification_rows": total_verification, "unique_verification_rows_absent_from_main": total_unique_absent, "failure_observation_rows": total_failures, "pr_747_duplicate_rows_retained": 346}

def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("repo", type=pathlib.Path)
    parser.add_argument("archive", nargs="?", type=pathlib.Path)
    args = parser.parse_args()
    repo = args.repo.resolve()
    archive = (args.archive or repo / "historical-evidence/runtime-freshness-goal-g").resolve()
    try:
        result = verify(repo, archive)
    except (AssertionError, OSError, subprocess.CalledProcessError, KeyError, TypeError, ValueError) as exc:
        print(f"FAIL historical evidence verification: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(result, sort_keys=True))
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
