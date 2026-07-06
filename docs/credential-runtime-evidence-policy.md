# Credential Runtime Evidence Policy

This document defines the secret-safe runtime evidence boundary for #344 and #364.

Default registry CI is secret-free. It validates source profiles, runtime plans, remediation evidence, and `reports/credential-runtime-evidence-policy.json` without requiring API keys. CI must not fail because a data.go.kr, ECOS, KOSIS, Open Assembly, or Seoul Open Data credential is absent.

Credential-gated checks are operator opt-in work. Operators inject credentials through environment variables named in `reports/credential-runtime-evidence-policy.json`, run the bounded candidate batch for one source, and write a local receipt under `.datapan/runtime-evidence/`. Receipts may record credential presence, source id, candidate batch, outcome, error class, timestamps, and response metadata needed for routing. Receipts must not record credential values, credential hashes, authorization headers, or service keys.

Checked-in registry releases remain canonical-registry compatible while live credentialed receipts are absent. The remaining `credential_required`, `metadata_only_verification`, and `non_data_runtime_evidence_not_collected` findings stay manual-review boundaries until reviewed receipts are linked from source runtime remediation evidence.

Local secret-free check:

```bash
python3 scripts/generate-credential-runtime-evidence-policy.py --check
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

Do not commit `.datapan/runtime-evidence/` receipts until the receipt schema and redaction review are explicitly added to the release ledger.
