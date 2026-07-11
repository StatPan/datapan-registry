# Registry Shard Artifact Strategy

`data/data-go-kr.registry.json` remains the canonical compatibility artifact
for released Datapan registry snapshots. That path is intentionally stable for
CLI, SDK, agent, Studio, GitHub Release, and downstream install consumers.

The same monolithic artifact is a poor development unit as the registry grows:
it is expensive to materialize in every workflow, opaque in pull request
review, and too coarse for source, institution, provider, or host scoped
verification. This document defines the staged strategy for adding shard-aware
registry artifacts without removing the canonical snapshot.

## Goals

- Preserve the current full-registry release and install path.
- Make scoped registry changes reviewable without inspecting a large LFS blob.
- Allow CI jobs to validate scoped artifacts when they do not need the full
  registry.
- Keep full release verification able to prove the canonical artifact.
- Make every generated shard accountable through checksums, provenance, and a
  manifest-bound inventory.
- Give `datapan-cli` and downstream consumers a migration path that can prefer
  shards while falling back to the canonical monolith.

## Non-Goals

- Do not remove `data/data-go-kr.registry.json`.
- Do not change normalized registry record semantics.
- Do not replace release verification with shard-only verification.
- Do not require runtime execution, provider adapters, or dataset storage in
  this repository.
- Do not hand-edit generated release artifacts such as `manifest.json` or
  `schemas/index.json`.

## Current Baseline

The current `data.go.kr` registry materializes to about 137 MB through Git LFS.
The release workflows correctly check that the file is materialized before
running full release verification. That should remain the release gate until
consumers support a shard-aware install path.

The repository already has source-scoped planning in
`docs/multi-source-release-layout.md`. That layout separates sources and
reports. It does not by itself solve the large single-source registry problem,
because `data.go.kr` can still dominate clone, review, and CI time even after
other sources are split into their own registry files.

## Initial Shard Dimension

The first shard dimension should be:

```text
source_id -> institution
```

For `data.go.kr`, that means deterministic institution-scoped shards generated
from `data/data-go-kr.registry.json`:

```text
data/
  data-go-kr.registry.json
  data-go-kr/
    shards/
      by-institution/
        <institution-slug>.registry.json
      registry-shards.json
```

Institution is the first key because current registry operations are already
planned and measured at that level:

- `reports/data-go-kr/coverage-backlog.json` ranks institution gaps.
- `reports/data-go-kr/institution-api-overview.json` is the main API and
  operation surface overview.
- `reports/data-go-kr/institution-runtime-plan.json` turns gaps into bounded
  `datapan catalog verify --org` batches.
- Link-detail and operation materialization work already lands as
  institution-focused batches.

Provider or host shards are useful for external adapter work, but they are not
the right primary split for the first phase. The same institution can involve
gateway operations and many external hosts, and the `apis.data.go.kr` gateway
host would stay too large if host were the only primary key. Provider and host
lookups should be recorded as indexes over the institution shards instead of
duplicating registry records into multiple primary shard sets.

## Shard Inventory

The first generated contract should be a shard inventory, not a hand-maintained
directory convention. `datapan.registry-shards.v1` records:

- `schema_version`
- `generated_at`
- `source_id`
- `provider`
- `source_registry`
- `source_registry_sha256`
- `strategy`, initially `by_institution`
- shard count, total records, total bytes, and aggregate checksum
- shard entries with path, institution name, stable shard key, record count,
  byte count, sha256, and optional provider/host summaries
- generation inputs and generator version
- recomposition policy

The inventory should be included in the release manifest as a required
`registry_shards` artifact once it exists. Shard files can be protected in
either of two ways:

1. Include every shard file directly in `manifest.json`.
2. Include a manifest-bound shard inventory whose entries carry every shard
   checksum, then teach release verification to validate the inventory and the
   referenced shard files.

The second option is preferable once `datapan-cli` supports it because it keeps
the root manifest readable while still making every shard checksum verifiable.
Until that verifier exists, implementation tickets should either keep shards
outside the release gate or list shard files directly in the generated
manifest.

## Publication Policy

The first shard publication mode is a GitHub Release asset, not checked-in Git
files. The canonical LFS registry remains the checked-in compatibility artifact.

Measured on the current `data.go.kr` registry:

| Artifact | Size |
| --- | ---: |
| canonical `data/data-go-kr.registry.json` | 137,735,169 bytes |
| generated institution shard JSON files | 137,736,399 bytes |
| generated shard directory on disk | 137,937,097 bytes |
| generated shard tar.gz archive | 7,989,462 bytes |
| canonical registry gzip archive | 9,415,820 bytes |

These measurements rule out committing shard JSON files as ordinary Git blobs:
that would add another registry-sized payload to every checkout. Git LFS shards
would avoid normal Git blobs, but they would still multiply LFS pointer churn
and require a policy for hundreds of generated files. A compressed GitHub
Release asset gives consumers a smaller optional download without increasing
default clone or pull cost.

During the compatibility period:

- `data/data-go-kr.registry.json` remains the checked-in canonical registry.
- `reports/release-consumer-compatibility.json` records this compatibility
  boundary as manifest-bound release evidence for downstream consumers.
- shard files are generated and validated from the canonical registry.
- `data-go-kr-shards.tar.gz` is the intended first release asset name for the
  generated `data/data-go-kr/shards/` tree.
- `registry-shards.json` inside that archive is the shard checksum inventory.
- root `manifest.json` must not require shard files until release verification
  can validate manifest-bound shard inventories and downstream consumers support
  monolith fallback.
- shard publication is blocked on the consumer fallback work tracked by #245.

Before publishing shard archives in a release, CI must prove:

- full `Verify registry release` still passes with the canonical LFS registry;
- `scripts/generate-registry-shards.py` can generate the shard tree from the
  materialized canonical registry;
- `scripts/validate-registry-shards.py` passes on the generated inventory;
- `scripts/package-registry-shards.py` can package the generated shard tree as
  `data-go-kr-shards.tar.gz` and inspect the archive shape;
- release install and doctor checks remain green through the canonical path;
- downstream consumers have a shard-preferred, monolith-fallback path.

The repeatable packaging command is:

```bash
python scripts/package-registry-shards.py \
  --shard-dir data/data-go-kr/shards \
  --output .datapan/release-assets/data-go-kr-shards.tar.gz
python scripts/package-registry-shards.py \
  --check .datapan/release-assets/data-go-kr-shards.tar.gz
```

The archive root contains `registry-shards.json` plus shard paths exactly as
listed in the inventory, for example `by-institution/<key>.registry.json`.
Packaging remains optional during the compatibility period and does not make
shard artifacts required by `manifest.json` or release readiness.

The manual `Draft registry release` workflow can generate the same archive from
the materialized canonical registry and upload it under
`.datapan/release-assets/data-go-kr-shards.tar.gz` as publish-prep evidence.
It also packages `.datapan/release-assets/datapan-registry-snapshot.zip` and
checks that the shard archive's `source_registry_sha256` matches the registry
inside that installable zip. That workflow still does not publish a GitHub
Release or make shard artifacts required; a human release operator must attach
the matching assets deliberately.

## Recomposition Invariant

Shards are a derived representation of the canonical registry. Validation must
prove that they do not change registry semantics.

Minimum invariant:

- every canonical registry record appears in exactly one primary shard;
- no shard record is absent from the canonical registry;
- stable serialization of all shard records in canonical order has the same
  logical record set as the canonical registry;
- shard inventory totals match the canonical registry totals;
- shard checksums match the files on disk;
- shard generation is deterministic for the same canonical input.

The validator should fail on duplicate records, missing records, unstable
ordering, stale checksums, or drift between the inventory and shard files.

## CI Model

Full LFS materialization should remain required for:

- `Verify registry release`
- release draft verification
- tag verification
- install smoke tests
- any workflow proving `datapan catalog release verify`
- any workflow proving the GitHub Release asset remains installable

Shard-aware validation can later be used for:

- documentation and schema-only pull requests that do not need registry
  contents;
- source-scoped report validation once the relevant source shard is available;
- institution runtime plan checks that only need selected institution shards;
- provider or host scoped verification planning through shard inventory
  indexes;
- pull request summaries showing changed institutions, providers, hosts, and
  operation counts without diffing the LFS artifact.

CI should not claim release readiness from shard-only checks until the release
verifier proves the canonical artifact and shard inventory together.

## Review Model

Generated shard files should make registry changes inspectable at the same
scope where work is planned. A materialization PR should be able to show:

- changed institution shards;
- API and operation counts before and after;
- changed provider and host summaries;
- whether the canonical monolith was regenerated from the same inputs;
- whether recomposition still matches the canonical registry.

Large LFS diffs can remain opaque as long as the shard inventory and scoped
shards provide the semantic review surface.

## Migration Stages

### Stage 1: Strategy

Document this plan, keep canonical behavior unchanged, and create executable
implementation tickets.

### Stage 2: Inventory Contract

Add `datapan.registry-shards.v1` and a generated
`data/data-go-kr/shards/registry-shards.json` inventory. Validate the inventory
without changing release install behavior.

### Stage 3: Shard Generation

Generate deterministic institution shards from the canonical registry. Keep
the monolith as the source of truth. Validate inventory, checksums, and
recomposition.

The generator entrypoint is:

```bash
python scripts/generate-registry-shards.py --output-dir data/data-go-kr/shards --clean
```

During the initial rollout, use a temporary output directory such as
`.datapan/shard-check` for validation runs. Check in shard artifacts only after
the shard inventory, recomposition validator, and CI policy agree on whether
the generated shard files are release artifacts, review artifacts, or both.

Validate a generated inventory and its shards with:

```bash
python scripts/validate-registry-shards.py .datapan/shard-check/registry-shards.json
```

### Stage 4: CI Split

Keep full release verification on LFS. Add scoped checks that can use shard
metadata for review and planning jobs. Document which workflows are scoped and
which remain full-release gates.

### Stage 5: Consumer Fallback

Update `datapan-cli` and downstream consumers outside this repository to prefer
the shard inventory where useful and fall back to
`data/data-go-kr.registry.json` for compatibility.

The downstream compatibility contract is:

- `datapan catalog install datapan-registry` continues to install a usable
  canonical registry when only `data/data-go-kr.registry.json` is present.
- release install may optionally download `data-go-kr-shards.tar.gz` after the
  canonical registry is available, but shard download failure must not break
  existing monolith installs during the compatibility period.
- `datapan doctor --json` continues to validate the canonical registry path and
  may report shard inventory health only as additive metadata until shard
  consumption is required by a future release policy.
- `datapan catalog release verify` keeps validating the full manifest and
  canonical registry. Shard inventory validation may be added as an extra check
  only after the verifier understands `registry_shards` artifacts.
- scoped commands such as institution verification planning, source-scoped
  report validation, provider or host planning, and pull request summaries may
  prefer shard inventories because they benefit from partial reads.
- every shard-preferred path must fall back to the canonical registry when
  shard archives or inventories are missing, stale, or unsupported.

Affected `datapan-cli` surfaces:

- `catalog install`
- `doctor`
- `catalog release verify`
- `catalog release readiness`
- `catalog verify --org`
- `catalog verify --provider`
- catalog diff/audit/report commands that only need scoped registry slices

Shard-preferred release consumption is proven with monolith fallback and no
regression in existing install behavior by completed downstream work
`StatPan/datapan-cli#128` and merged PR `StatPan/datapan-cli#129`. Shards remain
optional and the canonical registry remains required during the compatibility
period.

### Stage 6: Release Asset Policy

Publish release assets that include the canonical registry and shard inventory.
Only consider shard-first or monolith-optional release assets after install,
doctor, release verify, and downstream consumers support the new contract.

## Compatibility Risks

- `datapan-cli` release install currently expects the canonical registry path.
- `datapan doctor` and release health checks prove installed registry behavior
  through the canonical artifact.
- GitHub Release assets are documented as a way to consume the snapshot without
  relying on Git LFS.
- Downstream consumers may pin `data/data-go-kr.registry.json` directly.
- Manifests and readiness reports are generated artifacts and must not be
  manually edited to include shards.

Those risks mean the first implementation must be additive. The compatibility
path should be deprecated only through a future major release policy after
consumer support exists and is verified.

## Follow-Up Work Packets

The strategy should be implemented through bounded tickets:

1. Add a `datapan.registry-shards.v1` schema and generated shard inventory
   contract.
2. Generate deterministic data.go.kr institution shards from the canonical
   registry.
3. Validate shard inventory checksums and recomposition invariants.
4. Add scoped CI checks that use shard metadata without weakening release
   verification.
5. Update release cadence and README once shard artifacts are generated and
   verified.
6. Coordinate `datapan-cli` support for shard-preferred, monolith-fallback
   consumption.
