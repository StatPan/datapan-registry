# Source Runtime Readiness

This overview is generated from `reports/source-runtime-evidence-rollup.json` and its checked-in source runtime evidence plans. Regenerate it with `python scripts/generate-source-runtime-readiness.py` after updating a source runtime plan or rollup.

- Generated at: `2026-07-06T17:46:39Z`
- Sources: `5`
- Sources without runtime evidence: `4`
- Runtime evidence total: `4774`
- Verified: `2841`
- Failed: `389`
- Skipped: `1544`
- Unknown: `0`
- Blocking blocker instances: `9`
- Warning instances: `4`

## Source Summary

| Source | Source ID | Evidence | Blockers | Warnings | Blocker IDs | Warning IDs |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| data.go.kr | `data_go_kr` | 4774 | 1 | 0 | `credential_required` |  |
| ECOS | `ecos` | 0 | 2 | 1 | `credential_required`, `metadata_only_verification` | `non_data_runtime_evidence_not_collected` |
| KOSIS | `kosis` | 0 | 2 | 1 | `credential_required`, `metadata_only_verification` | `non_data_runtime_evidence_not_collected` |
| open.assembly.go.kr | `open_assembly` | 0 | 2 | 1 | `credential_required`, `metadata_only_verification` | `non_data_runtime_evidence_not_collected` |
| data.seoul.go.kr | `seoul_open_data` | 0 | 2 | 1 | `credential_required`, `metadata_only_verification` | `non_data_runtime_evidence_not_collected` |

## Blockers By ID

| Blocker ID | Count | Sources |
| --- | ---: | ---: |
| credential_required | 5 | `data_go_kr`, `ecos`, `kosis`, `open_assembly`, `seoul_open_data` |
| metadata_only_verification | 4 | `ecos`, `kosis`, `open_assembly`, `seoul_open_data` |

## Warnings By ID

| Warning ID | Count | Sources |
| --- | ---: | ---: |
| non_data_runtime_evidence_not_collected | 4 | `ecos`, `kosis`, `open_assembly`, `seoul_open_data` |

## Source Next Actions

### data.go.kr (`data_go_kr`)

- Runtime evidence: `4774`
- Verification mode: `bounded_call`
- Adapter status: `registered`
- Credential required: `true`
- Candidate batch: `reports/data-go-kr/runtime-candidates.json`
- First batch policy: Use the pinned candidate batch to run a small credential-gated gateway check, while preserving the broader latest-verification evidence as the release-wide runtime baseline.
- Promotion gate: Do not treat data.go.kr runtime coverage as complete until credentialed checks are repeatable and source-specific error taxonomy is verified.

Required CLI capabilities:

- `source credential injection`
- `credential-safe runtime receipt policy`
- `bounded gateway verification batches`

Required source reports:

- `reports/data-go-kr/coverage-backlog.json`
- `reports/data-go-kr/runtime-candidates.json`
- `reports/credential-runtime-evidence-policy.json`
- `reports/latest-verification.json`
- `reports/latest-verification-summary.json`

Open blockers:

- `credential_required` (operator): Provide a non-secret serviceKey injection path for repeatable bounded data.go.kr runtime checks in local and CI release operations.

Warnings:

- None recorded


### ECOS (`ecos`)

- Runtime evidence: `0`
- Verification mode: `metadata_only`
- Adapter status: `registered`
- Credential required: `true`
- Candidate batch: `reports/ecos/runtime-candidates.json`
- First batch policy: Run a credential-gated bounded sample-call batch only after ECOS statCode/cycle/date-window/itemCode parameters are pinned.
- Promotion gate: Do not promote ECOS datasets beyond registry_only until runtime evidence and time-series mapping contracts exist.

Required CLI capabilities:

- `runtime candidate batch ingestion`
- `source credential injection`
- `credential-safe runtime receipt policy`
- `ECOS error signature extraction`

Required source reports:

- `reports/ecos/coverage.json`
- `reports/ecos/latest-verification.json`
- `reports/ecos/latest-verification-summary.json`
- `reports/ecos/verification-plan.json`
- `reports/credential-runtime-evidence-policy.json`

Open blockers:

- `metadata_only_verification` (datapan_cli): Promote ECOS from metadata-only to bounded sample-call verification after statCode, cycle, date-window, and item samples are pinned.
- `credential_required` (operator): Define a non-secret API key injection path for source-scoped CI and local bounded checks.

Warnings:

- `non_data_runtime_evidence_not_collected`: Use reports/ecos/runtime-candidates.json with a registered adapter and credentials to run the first bounded verification batch.


### KOSIS (`kosis`)

- Runtime evidence: `0`
- Verification mode: `metadata_only`
- Adapter status: `registered`
- Credential required: `true`
- Candidate batch: `reports/kosis/runtime-candidates.json`
- First batch policy: Run a credential-gated bounded sample-call batch only after sample orgId/tblId/statId/period parameters are pinned.
- Promotion gate: Do not promote KOSIS datasets beyond registry_only until runtime evidence and table mapping contracts exist.

Required CLI capabilities:

- `runtime candidate batch ingestion`
- `source credential injection`
- `credential-safe runtime receipt policy`
- `KOSIS error signature extraction`

Required source reports:

- `reports/kosis/coverage.json`
- `reports/kosis/latest-verification.json`
- `reports/kosis/latest-verification-summary.json`
- `reports/kosis/verification-plan.json`
- `reports/credential-runtime-evidence-policy.json`

Open blockers:

- `metadata_only_verification` (datapan_cli): Promote KOSIS from metadata-only to bounded sample-call verification after sample parameters and credential policy are pinned.
- `credential_required` (operator): Define a non-secret API key injection path for source-scoped CI and local bounded checks.

Warnings:

- `non_data_runtime_evidence_not_collected`: Use reports/kosis/runtime-candidates.json with a registered adapter and credentials to run the first bounded verification batch.


### open.assembly.go.kr (`open_assembly`)

- Runtime evidence: `0`
- Verification mode: `metadata_only`
- Adapter status: `registered`
- Credential required: `true`
- Candidate batch: `reports/open-assembly/runtime-candidates.json`
- First batch policy: Run a credential-gated bounded sample-call batch only after service IDs and required legislative parameters are pinned.
- Promotion gate: Do not promote Open Assembly datasets beyond registry_only until runtime evidence and legislative identity mappings exist.

Required CLI capabilities:

- `runtime candidate batch ingestion`
- `source credential injection`
- `credential-safe runtime receipt policy`
- `Open Assembly RESULT code extraction`

Required source reports:

- `reports/open-assembly/coverage.json`
- `reports/open-assembly/latest-verification.json`
- `reports/open-assembly/latest-verification-summary.json`
- `reports/open-assembly/verification-plan.json`
- `reports/credential-runtime-evidence-policy.json`

Open blockers:

- `metadata_only_verification` (datapan_cli): Promote Open Assembly from metadata-only to bounded sample-call verification after service IDs and legislative parameters are pinned.
- `credential_required` (operator): Define a non-secret KEY injection path for source-scoped CI and local bounded checks.

Warnings:

- `non_data_runtime_evidence_not_collected`: Use reports/open-assembly/runtime-candidates.json with a registered adapter and credentials to run the first bounded verification batch.


### data.seoul.go.kr (`seoul_open_data`)

- Runtime evidence: `0`
- Verification mode: `metadata_only`
- Adapter status: `registered`
- Credential required: `true`
- Candidate batch: `reports/seoul-open-data/runtime-candidates.json`
- First batch policy: Run a credential-gated bounded sample-call batch only after service/start_index/end_index/format parameters are pinned.
- Promotion gate: Do not promote Seoul Open Data datasets beyond registry_only until runtime evidence and service-specific row schema contracts exist.

Required CLI capabilities:

- `runtime candidate batch ingestion`
- `source credential injection`
- `credential-safe runtime receipt policy`
- `Seoul RESULT code extraction`

Required source reports:

- `reports/seoul-open-data/coverage.json`
- `reports/seoul-open-data/latest-verification.json`
- `reports/seoul-open-data/latest-verification-summary.json`
- `reports/seoul-open-data/verification-plan.json`
- `reports/credential-runtime-evidence-policy.json`

Open blockers:

- `metadata_only_verification` (datapan_cli): Promote Seoul Open Data from metadata-only to bounded sample-call verification after service and index parameters are pinned.
- `credential_required` (operator): Define a non-secret KEY injection path for source-scoped CI and local bounded checks.

Warnings:

- `non_data_runtime_evidence_not_collected`: Use reports/seoul-open-data/runtime-candidates.json with a registered adapter and credentials to run the first bounded verification batch.

