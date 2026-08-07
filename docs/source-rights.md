# Source rights boundary

## What Apache-2.0 covers

The repository [LICENSE](../LICENSE) grants Apache-2.0 permissions only for
original Datapan-authored code, scripts, documentation, schemas, examples, and
other original Registry material. The [NOTICE](../NOTICE) records the same
boundary for redistributors.

The license does **not** grant rights in upstream datasets, API responses,
provider documentation, provider marks, credentials, or other third-party
material that is stored in, referenced by, or reachable through the Registry.

## Upstream terms remain controlling

For every source, the applicable provider's current terms, attribution
requirements, access conditions, and redistribution limits control. The
Registry may preserve identifiers, metadata, provenance, snapshots, or
verification evidence so consumers can trace a source. That evidence is not an
upstream-data sublicense and must not be treated as permission to collect,
reuse, publish, or redistribute upstream material.

Check the source-specific record before each use or redistribution decision:

- [`sources/`](../sources/) records official source references, including
  `terms_url` where it has been reviewed and recorded;
- [`provenance/`](../provenance/) records how a release was produced from a
  source; for example, [data.go.kr release provenance](../provenance/data-go-kr.md);
- the generated
  [`reports/source-contract-rollup.json`](../reports/source-contract-rollup.json)
  exposes the source-reference and terms-URL coverage that release checks use;
- source-specific provenance policies and release artifacts can record a
  conditional rights assessment, required attribution, prohibited uses, and
  revalidation triggers. For example, the KOSIS regional-baseline policy keeps
  its terms URL, attribution requirement, and unchanged-raw-paid-
  redistribution restriction in
  [`policy/kosis-regional-baseline-v0-provenance.json`](../policy/kosis-regional-baseline-v0-provenance.json).

If a terms record is absent, stale, ambiguous, or inconsistent with a proposed
use, stop and obtain or record the applicable source terms. Do not infer a
redistribution right from public accessibility, an API key, a Registry entry,
or Apache-2.0.

## Maintainer and contributor rules

- Do not relabel upstream data as Apache-2.0 or add provider material to a
  release merely because repository code is Apache-2.0.
- Preserve source attribution and official reference links in source profiles
  and provenance records.
- Treat a change to provider terms, attribution, source identity, or a
  table-specific notice as a review trigger, not as a metadata-only cleanup.
- Keep credentials and access entitlements out of the repository.

This document is an operational boundary, not legal advice. It does not change
any provider's terms or grant permission on that provider's behalf.
