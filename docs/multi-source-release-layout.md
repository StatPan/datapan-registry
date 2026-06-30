# Multi-Source Registry Release Layout

`datapan-registry` currently publishes a `data.go.kr`-centered release
snapshot. The repository should be able to add source-specific registries
without breaking current consumers or turning the root layout into a collection
of unrelated dumps.

This document defines the target layout contract. It does not migrate generated
artifacts by itself.

See `docs/source-standardization-research.md` for the source survey policy and
official reference anchors that should inform each source-specific profile.
See `docs/registry-ops-scope.md` for the selected near-term scope and
repository ownership boundaries.

## Goals

- Preserve the current `data/data-go-kr.registry.json` path for existing CLI,
  SDK, agent, Studio, and release-install consumers.
- Give every source a stable identifier that can be used by manifests,
  reports, provenance, verification evidence, and downstream impact plans.
- Keep source-specific evidence grouped enough that provider failures can be
  triaged without scanning unrelated release artifacts.
- Allow a root release manifest to describe one or more source registries in a
  single publishable release.

## Source Identity

Each source gets two names:

- `provider`: the upstream provider name used in existing reports, such as
  `data.go.kr`.
- `source_id`: a filesystem and API stable identifier, such as `data_go_kr`,
  `open_assembly`, `kosis`, or `ecos`.

Use `source_id` for paths and machine joins. Keep `provider` for human-facing
labels and compatibility with current registry records.

## Layout

The current registry path remains valid:

```text
data/
  data-go-kr.registry.json
```

New source registries should use source-specific names under `data/`:

```text
data/
  data-go-kr.registry.json
  open-assembly.registry.json
  kosis.registry.json
  ecos.registry.json
```

Reports should move toward source-scoped directories:

```text
reports/
  coverage.json
  latest-verification.json
  latest-verification-summary.json
  data-go-kr/
    coverage.json
    catalog-audit.json
    catalog-diff.json
    error-catalog.json
    provider-backlog.json
    route-disposition.json
    verification-plan.json
    latest-verification.json
    latest-verification-summary.json
  open-assembly/
    coverage.json
    catalog-audit.json
    error-catalog.json
    latest-verification.json
    latest-verification-summary.json
```

Root-level reports remain release-wide rollups. Source directories carry the
source-specific evidence used to produce those rollups.

Provenance should follow the same rule:

```text
provenance/
  data-go-kr.md
  open-assembly.md
  kosis.md
  ecos.md
```

## Compatibility

`data/data-go-kr.registry.json` is the compatibility path and must remain
available until a future major release explicitly deprecates it. If a later
layout introduces `data/data-go-kr/registry.json`, the old path should remain
as either the canonical artifact or an explicit compatibility copy.

Existing root-level files should keep their current meaning:

- `manifest.json`: release manifest for the published snapshot.
- `schemas/index.json`: schema index for schemas included in the published
  snapshot.
- `reports/latest-release-verification.json`: release-wide verification of the
  manifest.
- `reports/latest-release-readiness.json`: release-wide readiness gates.

## Manifest Representation

The current manifest schema can list multiple registry artifacts because
artifact paths are not provider-specific. Future `datapan.release-manifest.v1`
producers should include source-specific registry artifacts and source-scoped
reports in the same `artifacts` array:

```json
{
  "path": "data/data-go-kr.registry.json",
  "kind": "registry",
  "schema": "https://schemas.datapan.dev/datapan.specs.v1.schema.json"
}
```

```json
{
  "path": "data/open-assembly.registry.json",
  "kind": "registry",
  "schema": "https://schemas.datapan.dev/datapan.specs.v1.schema.json"
}
```

The manifest should also gain a source inventory in a future schema version if
consumers need to distinguish release-wide provider metadata from per-source
metadata without scanning artifact paths.

## Report Grouping

Source-scoped reports should use the same schema contracts as current
root-level reports. The report body should carry both `provider` and, where the
schema supports it, `source_id`.

Recommended source-scoped reports:

- `coverage.json`
- `catalog-audit.json`
- `catalog-diff.json`
- `dependencies.json`
- `error-catalog.json`
- `provider-backlog.json`
- `adapter-targets.json`
- `route-disposition.json`
- `verification-plan.json`
- `runtime-evidence-plan.json`
- `latest-verification.json`
- `latest-verification-summary.json`

Root rollups should be generated from source-scoped reports and should not hide
the source that produced a warning, failure, or skipped verification result.

## GitHub Actions

Release workflows should move from hard-coded provider lists to generated
source/provider matrices. The matrix should be derived from checked-in registry
metadata, provider profile files, or a CLI-generated plan artifact.

The current `Verify registry release` workflow remains the compatibility gate
for the existing `data.go.kr` release surface.

## Non-Goals

- Do not move generated JSON artifacts as part of this design note.
- Do not remove `data/data-go-kr.registry.json`.
- Do not require automatic release publishing.
- Do not add private source credentials or source-specific secrets to the
  repository.
