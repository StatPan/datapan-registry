# Credential Runtime Evidence Policy

This document defines the secret-safe runtime evidence boundary for #344, #364,
#366, #369, #371, #373, #375, and #379.

Default registry CI is secret-free. It validates source profiles, runtime plans, remediation evidence, `reports/credential-runtime-evidence-policy.json`, `reports/credential-runtime-receipt-collection-queue.json`, and the redacted receipt contract without requiring API keys. CI must not fail because a data.go.kr, ECOS, KOSIS, Open Assembly, or Seoul Open Data credential is absent.

Credential-gated checks are operator opt-in work. Operators inject credentials through environment variables named in `reports/credential-runtime-evidence-policy.json`, run the bounded candidate batch for one source, and write a local staging receipt under `.datapan/runtime-evidence/`. Staged receipts must conform to `schemas/datapan.credential-runtime-receipt.v1.schema.json` and pass `python3 scripts/validate-credential-runtime-receipts.py --allow-unreviewed <receipt>`. They may record credential presence, source id, candidate batch, outcome, error class, timestamps, and response metadata needed for routing. Receipts must not record credential values, credential hashes, authorization headers, or service keys.

Reviewed checked-in receipts live under `reports/credential-runtime-receipts/`. Default secret-free CI validates that reviewed intake path with `python3 scripts/validate-credential-runtime-receipts.py`; checked-in receipts require `review` metadata with an allowed review state. The credential runtime policy generator discovers reviewed receipts from that path and derives receipt presence, validation, review, relief eligibility, and manual-review reduction state from the checked-in files. A reviewed receipt can become compatibility-relief eligible only when it is present, schema-valid, redaction-safe, reviewed, and accepted for relief.

The reviewed receipt collection queue is the operator-facing next-action report. It is generated from the credential runtime policy and checked-in reviewed receipts, lists every credential-gated source, records the staged and reviewed receipt paths, preserves the source-specific bounded runtime command, and classifies the current state as absent, staged-only, reviewed-rejected, reviewed-accepted, or relief-eligible. A queue entry does not reduce manual-review boundaries by itself.

Reviewed receipt promotion is deterministic. Operators use `scripts/promote-credential-runtime-receipt.py` to attach review metadata to a local staged receipt and write the policy-defined reviewed receipt path. The command validates the staged receipt, redaction contract, source policy alignment, review decision semantics, and reviewed receipt schema before it writes output.

Source runtime remediation findings link back to reviewed receipt paths. `reports/source-runtime-remediation-map.json` records the expected reviewed receipt artifact and current receipt state for each credential-related manual-review boundary so operators can see which finding is waiting for which reviewed receipt.

Credential runtime collection has a local runner. `scripts/run-credential-runtime-collection.py` reads the checked-in queue, verifies candidate paths and credential environment presence without printing secret values, and can run a selected bounded source check only when the operator explicitly passes `--run`.

Checked-in registry releases remain canonical-registry compatible while live credentialed receipts are absent. The remaining `credential_required`, `metadata_only_verification`, and `non_data_runtime_evidence_not_collected` findings stay manual-review boundaries until reviewed receipts are linked from source runtime remediation evidence.

The receipt contract existing is not enough to reduce compatibility risk. Release
compatibility relief is explicitly blocked until a receipt is present, validated,
reviewed, relief-eligible, and linked through remediation evidence; current reports therefore keep
`manual_review_reduction_allowed` set to `false`.

Local secret-free check:

```bash
python3 scripts/generate-credential-runtime-evidence-policy.py --check
python3 scripts/validate-credential-runtime-receipts.py
python3 scripts/generate-credential-runtime-receipt-collection-queue.py --check
python3 -m py_compile scripts/promote-credential-runtime-receipt.py
python3 scripts/run-credential-runtime-collection.py --self-test
python3 scripts/run-credential-runtime-collection.py --check
```

Credential-gated operator pattern:

```bash
DATAPAN_<SOURCE>_API_KEY=<secret> datapan source runtime verify \
  --source <source_id> \
  --candidates <reports/source/runtime-candidates.json> \
  --bounded \
  --json \
  --output .datapan/runtime-evidence/<source>-credentialed-receipt.json
```

Credential-gated runner pattern:

```bash
python3 scripts/run-credential-runtime-collection.py --source <source_id> --json
python3 scripts/run-credential-runtime-collection.py --source <source_id> --require-env
python3 scripts/run-credential-runtime-collection.py --source <source_id> --run
```

Do not commit `.datapan/runtime-evidence/` receipts merely because the schema accepts them. A reviewed receipt must still be linked into source runtime remediation evidence before it can reduce a manual-review boundary.
