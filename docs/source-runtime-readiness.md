# Source Runtime Readiness

This overview is generated from `reports/source-runtime-evidence-rollup.json` and its checked-in source runtime evidence plans. Regenerate it with `python scripts/generate-source-runtime-readiness.py` after updating a source runtime plan or rollup.

- Generated at: `2026-06-30T09:16:13Z`
- Sources: `4`
- Sources without runtime evidence: `4`
- Runtime evidence total: `0`
- Verified: `0`
- Failed: `0`
- Skipped: `0`
- Unknown: `0`
- Blocking blocker instances: `11`
- Warning instances: `7`

## Source Summary

| Source | Source ID | Evidence | Blockers | Warnings | Blocker IDs | Warning IDs |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| ECOS | `ecos` | 0 | 3 | 2 | `adapter_not_registered`, `credential_required`, `metadata_only_verification` | `non_data_runtime_evidence_not_collected`, `source_runtime_adapter_not_registered` |
| KOSIS | `kosis` | 0 | 3 | 2 | `adapter_not_registered`, `credential_required`, `metadata_only_verification` | `non_data_runtime_evidence_not_collected`, `source_runtime_adapter_not_registered` |
| open.assembly.go.kr | `open_assembly` | 0 | 3 | 2 | `adapter_not_registered`, `credential_required`, `metadata_only_verification` | `non_data_runtime_evidence_not_collected`, `source_runtime_adapter_not_registered` |
| data.seoul.go.kr | `seoul_open_data` | 0 | 2 | 1 | `credential_required`, `metadata_only_verification` | `non_data_runtime_evidence_not_collected` |

## Blockers By ID

| Blocker ID | Count | Sources |
| --- | ---: | ---: |
| adapter_not_registered | 3 | `ecos`, `kosis`, `open_assembly` |
| credential_required | 4 | `ecos`, `kosis`, `open_assembly`, `seoul_open_data` |
| metadata_only_verification | 4 | `ecos`, `kosis`, `open_assembly`, `seoul_open_data` |

## Warnings By ID

| Warning ID | Count | Sources |
| --- | ---: | ---: |
| non_data_runtime_evidence_not_collected | 4 | `ecos`, `kosis`, `open_assembly`, `seoul_open_data` |
| source_runtime_adapter_not_registered | 3 | `ecos`, `kosis`, `open_assembly` |

## Source Next Actions

### ECOS (`ecos`)

- Runtime evidence: `0`
- Verification mode: `metadata_only`
- Adapter status: `none`
- Credential required: `true`
- Candidate batch: `reports/ecos/runtime-candidates.json`
- First batch policy: Run a credential-gated bounded sample-call batch only after ECOS statCode/cycle/date-window/itemCode parameters are pinned.
- Promotion gate: Do not promote ECOS datasets beyond registry_only until runtime evidence and time-series mapping contracts exist.

Required CLI capabilities:

- `runtime candidate batch ingestion`
- `ECOS bounded sample-call adapter`
- `source credential injection`
- `ECOS error signature extraction`

Required source reports:

- `reports/ecos/coverage.json`
- `reports/ecos/latest-verification.json`
- `reports/ecos/latest-verification-summary.json`
- `reports/ecos/verification-plan.json`

Open blockers:

- `metadata_only_verification` (datapan_cli): Promote ECOS from metadata-only to bounded sample-call verification after statCode, cycle, date-window, and item samples are pinned.
- `adapter_not_registered` (datapan_cli): Register an ECOS adapter with verification and call capabilities before runtime evidence can be generated.
- `credential_required` (operator): Define a non-secret API key injection path for source-scoped CI and local bounded checks.

Warnings:

- `non_data_runtime_evidence_not_collected`: Use reports/ecos/runtime-candidates.json with a registered adapter and credentials to run the first bounded verification batch.
- `source_runtime_adapter_not_registered`: Register the ECOS adapter before evidence collection.


### KOSIS (`kosis`)

- Runtime evidence: `0`
- Verification mode: `metadata_only`
- Adapter status: `none`
- Credential required: `true`
- Candidate batch: `reports/kosis/runtime-candidates.json`
- First batch policy: Run a credential-gated bounded sample-call batch only after sample orgId/tblId/statId/period parameters are pinned.
- Promotion gate: Do not promote KOSIS datasets beyond registry_only until runtime evidence and table mapping contracts exist.

Required CLI capabilities:

- `runtime candidate batch ingestion`
- `KOSIS bounded sample-call adapter`
- `source credential injection`
- `KOSIS error signature extraction`

Required source reports:

- `reports/kosis/coverage.json`
- `reports/kosis/latest-verification.json`
- `reports/kosis/latest-verification-summary.json`
- `reports/kosis/verification-plan.json`

Open blockers:

- `metadata_only_verification` (datapan_cli): Promote KOSIS from metadata-only to bounded sample-call verification after sample parameters and credential policy are pinned.
- `adapter_not_registered` (datapan_cli): Register a KOSIS adapter with verification and call capabilities before runtime evidence can be generated.
- `credential_required` (operator): Define a non-secret API key injection path for source-scoped CI and local bounded checks.

Warnings:

- `non_data_runtime_evidence_not_collected`: Use reports/kosis/runtime-candidates.json with a registered adapter and credentials to run the first bounded verification batch.
- `source_runtime_adapter_not_registered`: Register the KOSIS adapter before evidence collection.


### open.assembly.go.kr (`open_assembly`)

- Runtime evidence: `0`
- Verification mode: `metadata_only`
- Adapter status: `none`
- Credential required: `true`
- Candidate batch: `reports/open-assembly/runtime-candidates.json`
- First batch policy: Run a credential-gated bounded sample-call batch only after service IDs and required legislative parameters are pinned.
- Promotion gate: Do not promote Open Assembly datasets beyond registry_only until runtime evidence and legislative identity mappings exist.

Required CLI capabilities:

- `runtime candidate batch ingestion`
- `Open Assembly bounded sample-call adapter`
- `source credential injection`
- `Open Assembly RESULT code extraction`

Required source reports:

- `reports/open-assembly/coverage.json`
- `reports/open-assembly/latest-verification.json`
- `reports/open-assembly/latest-verification-summary.json`
- `reports/open-assembly/verification-plan.json`

Open blockers:

- `metadata_only_verification` (datapan_cli): Promote Open Assembly from metadata-only to bounded sample-call verification after service IDs and legislative parameters are pinned.
- `adapter_not_registered` (datapan_cli): Register an Open Assembly adapter with verification and call capabilities before runtime evidence can be generated.
- `credential_required` (operator): Define a non-secret KEY injection path for source-scoped CI and local bounded checks.

Warnings:

- `non_data_runtime_evidence_not_collected`: Use reports/open-assembly/runtime-candidates.json with a registered adapter and credentials to run the first bounded verification batch.
- `source_runtime_adapter_not_registered`: Register the Open Assembly adapter before evidence collection.


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
- `Seoul RESULT code extraction`

Required source reports:

- `reports/seoul-open-data/coverage.json`
- `reports/seoul-open-data/latest-verification.json`
- `reports/seoul-open-data/latest-verification-summary.json`
- `reports/seoul-open-data/verification-plan.json`

Open blockers:

- `metadata_only_verification` (datapan_cli): Promote Seoul Open Data from metadata-only to bounded sample-call verification after service and index parameters are pinned.
- `credential_required` (operator): Define a non-secret KEY injection path for source-scoped CI and local bounded checks.

Warnings:

- `non_data_runtime_evidence_not_collected`: Use reports/seoul-open-data/runtime-candidates.json with a registered adapter and credentials to run the first bounded verification batch.

