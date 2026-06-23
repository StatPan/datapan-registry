# datapan-registry

Versioned Datapan registry snapshots for public data APIs discovered from
data.go.kr.

This repository is the portable registry side of Datapan. It lets CLI, SDK,
agent, Studio, and downstream tooling consume a released registry without
re-importing the upstream data.go.kr catalog every time.

## Current Snapshot

- Provider: `data.go.kr`
- Specs: `12060`
- Registered external adapters: `epost`, `q-net`
- Release manifest: `manifest.json`
- Registry data: `data/data-go-kr.registry.json`
- Provider index: `data/provider-index.json`
- Schema index: `schemas/index.json`

`data/data-go-kr.registry.json` is stored with Git LFS because the normalized
registry is larger than GitHub's normal blob limit.

## Verify

From a checkout of `datapan-cli`, verify this snapshot with:

```bash
datapan catalog release verify --manifest manifest.json --output reports/latest-release-verification.json --json
datapan catalog release readiness --manifest manifest.json --output reports/latest-release-readiness.json --json
```

The current snapshot was generated from `datapan-cli` and includes:

- normalized data.go.kr registry;
- provider index for registered adapters;
- schema index and versioned schemas;
- catalog audit;
- error catalog;
- dependency inventory;
- adapter target work queue;
- provider backlog;
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
  catalog-audit.json
  error-catalog.json
  dependencies.json
  adapter-targets.json
  provider-backlog.json
  latest-release-verification.json
  latest-release-readiness.json
provenance/
  data-go-kr.md
manifest.json
```

## Release Policy

Use date-based tags such as `v2026.06.24`.

A release is publishable when:

- `manifest.json` verifies all required artifact checksums;
- required readiness gates pass;
- schema index and provider index are present;
- registry size and LFS handling are explicit;
- verification evidence is attached or documented;
- generated artifacts do not contain credentials.

See `docs/release-cadence.md` for the repeatable release loop.
