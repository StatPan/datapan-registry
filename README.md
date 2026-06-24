# datapan-registry

Versioned Datapan registry snapshots for public data APIs discovered from
data.go.kr.

This repository is the portable registry side of Datapan. It lets CLI, SDK,
agent, Studio, and downstream tooling consume a released registry without
re-importing the upstream data.go.kr catalog every time.

## Current Snapshot

- Provider: `data.go.kr`
- Specs: `12060`
- Latest release: `v2026.06.24.1`
- Registered external adapters: `epost`, `q-net`
- Runtime verification evidence: q-net and epost bounded external-provider
  runs merged into `reports/latest-verification.json`
- Release manifest: `manifest.json`
- Registry data: `data/data-go-kr.registry.json`
- Provider index: `data/provider-index.json`
- Schema index: `schemas/index.json`
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
release verification and readiness through `datapan-cli`, and smoke-tests that
the latest GitHub Release zip can be installed with
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
  qnet-verification.json
  epost-verification.json
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
