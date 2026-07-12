# Source Runtime Readiness

This overview is generated from `reports/source-runtime-evidence-rollup.json` and its checked-in source runtime evidence plans. Regenerate it with `python scripts/generate-source-runtime-readiness.py` after updating a source runtime plan or rollup.

- Generated at: `2026-07-06T17:46:39Z`
- Sources: `5`
- Sources without runtime evidence: `0`
- Runtime evidence total: `4778`
- Verified: `2845`
- Failed: `389`
- Skipped: `1544`
- Unknown: `0`
- Blocking blocker instances: `5`
- Warning instances: `0`

## Source Summary

| Source | Source ID | Evidence | Blockers | Warnings | Blocker IDs | Warning IDs |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| data.go.kr | `data_go_kr` | 4774 | 1 | 0 | `credential_required` |  |
| ECOS | `ecos` | 1 | 1 | 0 | `credential_required` |  |
| KOSIS | `kosis` | 1 | 1 | 0 | `credential_required` |  |
| open.assembly.go.kr | `open_assembly` | 1 | 1 | 0 | `credential_required` |  |
| data.seoul.go.kr | `seoul_open_data` | 1 | 1 | 0 | `credential_required` |  |

## Blockers By ID

| Blocker ID | Count | Sources |
| --- | ---: | ---: |
| credential_required | 5 | `data_go_kr`, `ecos`, `kosis`, `open_assembly`, `seoul_open_data` |

## Warnings By ID

| Warning ID | Count | Sources |
| --- | ---: | ---: |

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

- Runtime evidence: `1`
- Verification mode: `bounded_call`
- Adapter status: `registered`
- Credential required: `true`
- Candidate batch: `reports/ecos/runtime-candidates.json`
- First batch policy: Maintain the reviewed bounded ECOS sample-call receipt and revalidate it when pinned statCode, cycle, date-window, or item parameters change.
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

- `credential_required` (operator): Define a non-secret API key injection path for source-scoped CI and local bounded checks.

Warnings:

- None recorded


### KOSIS (`kosis`)

- Runtime evidence: `1`
- Verification mode: `bounded_call`
- Adapter status: `registered`
- Credential required: `true`
- Candidate batch: `reports/kosis/runtime-candidates.json`
- First batch policy: Maintain the reviewed bounded KOSIS sample-call receipt and revalidate it when pinned orgId, tblId, statId, or period parameters change.
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

- `credential_required` (operator): Define a non-secret API key injection path for source-scoped CI and local bounded checks.

Warnings:

- None recorded


### open.assembly.go.kr (`open_assembly`)

- Runtime evidence: `1`
- Verification mode: `bounded_call`
- Adapter status: `registered`
- Credential required: `true`
- Candidate batch: `reports/open-assembly/runtime-candidates.json`
- First batch policy: Maintain the reviewed bounded Open Assembly sample-call receipt and revalidate it when the service ID or legislative parameters change.
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

- `credential_required` (operator): Define a non-secret KEY injection path for source-scoped CI and local bounded checks.

Warnings:

- None recorded


### data.seoul.go.kr (`seoul_open_data`)

- Runtime evidence: `1`
- Verification mode: `bounded_call`
- Adapter status: `registered`
- Credential required: `true`
- Candidate batch: `reports/seoul-open-data/runtime-candidates.json`
- First batch policy: Maintain the reviewed bounded Seoul Open Data sample-call receipt and revalidate it when service or index parameters change.
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

- `credential_required` (operator): Define a non-secret KEY injection path for source-scoped CI and local bounded checks.

Warnings:

- None recorded

