# datapan-registry

Versioned Datapan registry snapshots for public data APIs discovered from
data.go.kr.

This repository is the portable registry side of Datapan. It lets CLI, SDK,
agent, Studio, and downstream tooling consume a released registry without
re-importing the upstream data.go.kr catalog every time.

## Current Snapshot

- Provider: `data.go.kr`
- Specs: `12060`
- Operations: `12205`
- Callable operations: `12063` (`98.8%`)
- Latest release: `v2026.06.25.3`
- Registered external adapters: `airport`, `ekape`, `epost`, `folk`, `forest`,
  `geoje`, `jeonju`, `q-net`, `uiryeong`, `ulsan`
- External adapter coverage: `418` registered-adapter operations out of `594`
  external endpoint operations (`68.2%`)
- Missing external adapter hosts: `27`
- Provider split readiness: `ready`
  (`10` adapters, `10` verification-capable, `5` call-capable)
- Runtime verification evidence: `91` bounded checks merged into
  `reports/latest-verification.json` (`16` verified, `24` failed, `51`
  skipped)
- Release manifest: `manifest.json`
- Registry data: `data/data-go-kr.registry.json`
- Provider index: `data/provider-index.json`
- Schema index: `schemas/index.json`
- Studio bundle schemas: `datapan.studio-datasets.v1`,
  `datapan.studio-bundle.v1`
- Catalog diff: `reports/catalog-diff.json`

`data/data-go-kr.registry.json` is stored with Git LFS because the normalized
registry is larger than GitHub's normal blob limit.

## Verify

From a checkout of `datapan-cli`, verify this snapshot with:

```bash
datapan catalog release verify --manifest manifest.json --output reports/latest-release-verification.json --json
datapan catalog release readiness --manifest manifest.json --output reports/latest-release-readiness.json --json
```

The same checks run in GitHub Actions on pushes, pull requests, version tags,
manual dispatches, and a weekly scheduled release-health check. The workflow
checks out this repository with Git LFS enabled, verifies that
`data/data-go-kr.registry.json` is materialized as the full registry file, runs
provider-specific verification summary checks, runs release verification and
readiness through `datapan-cli`, and smoke-tests that the latest GitHub Release
zip can be installed with
`datapan catalog install datapan-registry`.

The current snapshot was generated from `datapan-cli` and includes:

- normalized data.go.kr registry;
- provider index for registered adapters;
- schema index and versioned schemas;
- catalog diff against the previous published registry;
- catalog audit;
- error catalog;
- dependency inventory;
- adapter target work queue;
- provider backlog;
- latest merged verification evidence;
- latest verification summary;
- provenance.

## Layout

```text
schemas/
  index.json
  datapan.*.schema.json
data/
  data-go-kr.registry.json
  provider-index.json
reports/
  catalog-diff.json
  catalog-audit.json
  error-catalog.json
  dependencies.json
  adapter-targets.json
  provider-backlog.json
  airport-verification.json
  airport-verification-summary.json
  ekape-verification.json
  ekape-verification-summary.json
  epost-verification.json
  epost-verification-summary.json
  folk-verification.json
  folk-verification-summary.json
  forest-verification.json
  forest-verification-summary.json
  geoje-verification.json
  geoje-verification-summary.json
  jeonju-verification.json
  jeonju-verification-summary.json
  qnet-verification.json
  qnet-verification-summary.json
  uiryeong-verification.json
  uiryeong-verification-summary.json
  ulsan-verification.json
  ulsan-verification-summary.json
  latest-verification.json
  latest-verification-summary.json
  latest-release-verification.json
  latest-release-readiness.json
provenance/
  data-go-kr.md
manifest.json
```

## Release Policy

Use date-based tags such as `v2026.06.24`. If a second registry release is
needed on the same date, append a patch counter such as `v2026.06.24.1`.

A release is publishable when:

- `manifest.json` verifies all required artifact checksums;
- required readiness gates pass;
- recommended readiness gates pass, including catalog diff and verification
  evidence;
- schema index and provider index are present;
- registry size and LFS handling are explicit;
- verification evidence is attached or documented;
- generated artifacts do not contain credentials.
- the `Verify registry release` workflow passes for the commit or tag.
- the scheduled release-health workflow continues passing between releases.

See `docs/release-cadence.md` for the repeatable release loop.
