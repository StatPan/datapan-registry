# datapan-registry

Versioned Datapan registry snapshots for public data APIs discovered from
data.go.kr.

This repository is the portable registry side of Datapan. It lets CLI, SDK,
agent, Studio, and downstream tooling consume a released registry without
re-importing the upstream data.go.kr catalog every time.

## Current Snapshot

- Provider: `data.go.kr`
- Specs: `12060`
- Operations: `18826`
- Callable operations: `18684` (`99.2%`)
- Latest release: `v2026.06.25.24`
- Registered external adapters: `airport`, `andong`, `calspia`, `car`, `car365`, `chungnam`, `codil`, `consumer`, `culture`, `data-gg`,
  `dgfca`, `dongjak`, `ekape`, `emuseum`, `epost`, `eshare`, `ex`, `fairdata`, `folk`, `foodsafetykorea`, `forest`,
  `franchise-ftc`, `garak`, `gblib`, `geoje`, `gicoms`, `gimhae`, `gwanak`, `gwangjin`, `gwangmyeong`, `happysd`, `humetro`,
  `i815`, `icheon`, `ins24`, `itfind`, `its`, `jeju`, `jeju-air`, `jeju-www`, `jejudatahub`, `jejuits`, `jeonnam-redtable`, `jeonju`, `juso`,
  `kistep`, `kofpi`, `korad`, `kpx`, `lh-ebid`, `lofin365`, `mafra`, `mnd-open-data`, `myhome`, `nabic`, `naqs`, `ncpms`, `nfqs`,
  `nongsaro`, `oneclick-law`, `open-assembly`, `open-law`, `pqis`, `psis`, `q-net`, `safemap`, `safetydata`,
  `seoul-bus`, `seogu`, `seogwipo`, `seoul-open-data`, `sexoffender`, `sisul`, `sisul-www`, `stcis`, `tour`, `uiryeong`, `ulsan`, `vworld`,
  `wamis`, `work`, `work24`, `worldjob`, `cancer`
- External adapter coverage: `7206` registered-adapter operations out of `7216`
  external endpoint operations (`99.6%`)
- Missing external adapter hosts: `11`
- Provider split readiness: `ready`
  (`84` adapters, `84` verification-capable, `23` call-capable)
- Runtime verification evidence: `2312` bounded checks merged into
  `reports/latest-verification.json` (`903` verified, `212` failed, `1197`
  skipped)
- Runtime evidence growth target: `12.3%` checked evidence is above the
  unrounded `10%` release target; `0` additional records are required for this
  target.
- Institution API overview: `411` organizations, `12060` APIs, and `18826`
  operations in `reports/data-go-kr/institution-api-overview.json`; readable
  tables live in `docs/data-go-kr-institution-api-overview.md`.
- Missing external host probe: `81` manifest-bound probe records remain in
  `reports/unadapted-external-probe.json`; current route disposition consumes
  the `29` routes that are still missing adapters after registered hosts are
  excluded
- Route disposition: `29` missing external routes in
  `reports/route-disposition.json` (`14` dead-route candidates, `15`
  transient failures, `0` remaining adapter candidates)
- Coverage route evidence: `reports/coverage.json` now carries the same route
  evidence and reports `0` evidence-adjusted adapter candidates
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
- route disposition evidence for missing external routes;
- institution-level API and runtime-evidence overview;
- provider backlog;
- latest merged verification evidence;
- latest verification summary;
- manifest-bound unadapted external endpoint probe evidence;
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
  route-disposition.json
  provider-backlog.json
  andong-verification.json
  andong-verification-summary.json
  airport-verification.json
  airport-verification-summary.json
  culture-verification.json
  culture-verification-summary.json
  data-gg-verification.json
  data-gg-verification-summary.json
  ekape-verification.json
  ekape-verification-summary.json
  emuseum-verification.json
  emuseum-verification-summary.json
  epost-verification.json
  epost-verification-summary.json
  folk-verification.json
  folk-verification-summary.json
  forest-verification.json
  forest-verification-summary.json
  garak-verification.json
  garak-verification-summary.json
  happysd-verification.json
  happysd-verification-summary.json
  gblib-verification.json
  gblib-verification-summary.json
  geoje-verification.json
  geoje-verification-summary.json
  gwanak-verification.json
  gwanak-verification-summary.json
  humetro-verification.json
  humetro-verification-summary.json
  i815-verification.json
  i815-verification-summary.json
  itfind-verification.json
  itfind-verification-summary.json
  jeju-verification.json
  jeju-verification-summary.json
  jeonju-verification.json
  jeonju-verification-summary.json
  korad-verification.json
  korad-verification-summary.json
  kpx-verification.json
  kpx-verification-summary.json
  lh-ebid-verification.json
  lh-ebid-verification-summary.json
  mafra-verification.json
  mafra-verification-summary.json
  myhome-verification.json
  myhome-verification-summary.json
  naqs-verification.json
  naqs-verification-summary.json
  ncpms-verification.json
  ncpms-verification-summary.json
  nfqs-verification.json
  nfqs-verification-summary.json
  nongsaro-verification.json
  nongsaro-verification-summary.json
  oneclick-law-verification.json
  oneclick-law-verification-summary.json
  pqis-verification.json
  pqis-verification-summary.json
  qnet-verification.json
  qnet-verification-summary.json
  seoul-bus-verification.json
  seoul-bus-verification-summary.json
  seoul-open-data-verification.json
  seoul-open-data-verification-summary.json
  sisul-verification.json
  sisul-verification-summary.json
  tour-verification.json
  tour-verification-summary.json
  uiryeong-verification.json
  uiryeong-verification-summary.json
  ulsan-verification.json
  ulsan-verification-summary.json
  work24-verification.json
  work24-verification-summary.json
  unadapted-external-probe.json
  unadapted-external-probe-summary.json
  latest-verification.json
  latest-verification-summary.json
  latest-release-verification.json
  latest-release-readiness.json
  data-go-kr/
    institution-api-overview.json
    institution-runtime-plan.json
    coverage-backlog.json
    external-coverage-summary.json
    external-adapter-backlog.json
docs/
  data-go-kr-institution-api-overview.md
  data-go-kr-institution-runtime-plan.md
  data-go-kr-coverage-backlog.md
  data-go-kr-external-adapter-backlog.md
provenance/
  data-go-kr.md
manifest.json
```

## Coverage Targets

Datapan treats public-data coverage as an open-source operating target, not a
vague claim.

- Near term: reach `99%` callable operation coverage and `98%` external
  adapter operation coverage.
- Evidence target: reach `10%` operation-level runtime verification evidence
  while keeping provider-specific evidence under `reports/*-verification.json`
  before release.
- Adapter target: grow to at least `25` call-capable provider adapters and no
  more than `10` missing-adapter operations.
- Drift target: keep dead external routes documented through
  manifest-bound `reports/unadapted-external-probe.json` and
  `reports/route-disposition.json` evidence instead of treating them as
  unknown adapter work.

## Release Policy

Use date-based tags such as `v2026.06.24`. If a second registry release is
needed on the same date, append a patch counter such as `v2026.06.24.1`.

A release is publishable when:

- `manifest.json` verifies all required artifact checksums;
- required readiness gates pass;
- recommended readiness gates pass, including catalog diff and verification
  evidence;
- if coverage still has missing external adapter operations,
  unadapted external probe evidence and route disposition evidence are present
  as manifest-bound required artifacts;
- schema index and provider index are present;
- registry size and LFS handling are explicit;
- verification evidence is attached or documented;
- generated artifacts do not contain credentials.
- the `Verify registry release` workflow passes for the commit or tag.
- the scheduled release-health workflow continues passing between releases.

See `docs/release-cadence.md` for the repeatable release loop.
