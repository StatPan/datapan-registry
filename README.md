# datapan-registry

Versioned Datapan registry snapshots for public data APIs discovered from
data.go.kr.

This repository is the portable registry side of Datapan. It lets CLI, SDK,
agent, Studio, and downstream tooling consume a released registry without
re-importing the upstream data.go.kr catalog every time.

## Current Snapshot

- Provider: `data.go.kr`
- Specs: `12060`
- Operations: `21256`
- Callable operations: `21114` (`99.3%`)
- Sustainable coverage decision: `coverage_gaps` (`5` of `9` layers meet
  policy targets). Routable coverage is not treated as total usability.
- Supported-source denominator coverage: `5` of `5` sources have an explicit
  operation denominator (`100.0%`), covering `21260` operations in total.
- Runtime operation evidence: `4638` unique operation identities out of
  `21260` (`21.8%`); fresh successful evidence covers `2773` unique operations
  (`13.0%`) as of `2026-07-11T10:28:35Z`.
- Runtime freshness: `4070` evidence records are within the `30` day fresh
  window, `0` are stale, `0` are expired, and `712` missing timestamps are
  explicitly excluded from fresh coverage.
- Required consumer proof: `3` of `3` required consumers (`datapan-cli`,
  `release-operator`, `studio`) are proven (`100.0%`).
- Latest release: `v2026.06.25.24`
- Registered external adapters: `airport`, `andong`, `anyang`, `atfis`, `calspia`, `car`, `car365`, `childcare-info`, `chungbuk-tour`, `chungnam`, `codil`, `consumer`, `culture`, `daegu`, `daejeon`, `data-gg`,
  `dgfca`, `dongjak`, `ecos`, `ecvam`, `ekape`, `emuseum`, `epost`, `eshare`, `ex`, `fairdata`, `folk`, `foodsafetykorea`, `forest`,
  `franchise-ftc`, `garak`, `gblib`, `geoje`, `gicoms`, `gimhae`, `gims`, `gogung`, `gwanak`, `gwangjin`, `gwangmyeong`, `happysd`, `hrfco`, `hug`, `humetro`,
  `i815`, `icheon`, `incheon`, `ins24`, `ip-navi`, `itfind`, `its`, `iwest`, `jeju`, `jeju-air`, `jeju-www`, `jejudatahub`, `jejuits`, `jeonju`, `jeonnam-redtable`, `jongno`, `juso`,
  `keit`, `kipris-plus`, `khoa`, `kistep`, `kisti`, `kma-apihub`, `koagi`, `kofpi`, `kopis`, `korad`, `koreapost`, `koroad`, `kosis`, `kosmes`, `kpx`, `kric`, `lh-ebid`, `lofin365`, `mafra`, `mafra-legacy`, `milipass`, `mnd-open-data`, `mpva-egonghun`, `much`, `myhome`, `naa`, `nabic`, `naqs`, `ncpms`, `nfqs`, `nie-ecobank`,
  `nier-nesc`, `nihc`, `nrich`, `nrf`, `nongsaro`, `nosc`, `oneclick-law`, `open-assembly`, `open-law`, `openfiscaldata`, `opendart`, `pqis`, `psis`, `q-net`, `qia`, `recycling-info`, `safemap`, `safetydata`,
  `safe182`, `seogu`, `seogwipo`, `seoul-bus`, `seoul-map`, `seoul-open-data`, `seoul-tdata`, `sexoffender`, `sgis`, `sisul`, `sisul-www`, `smartfarm-korea`, `stcis`, `tashu`, `tour`, `uiryeong`, `ulsan`, `ulsan-www`, `unipass`, `utic`, `vworld`,
  `wamis`, `work`, `work24`, `worldjob`, `youthcenter`, `yuseong`, `cancer`
- External adapter coverage: `9636` registered-adapter operations out of `9646`
  external endpoint operations (`99.7%`)
- Missing external adapter hosts: `11`
- Provider split readiness: `ready`
  (`138` adapters, `138` verification-capable, `23` call-capable)
- Runtime verification evidence: `4782` bounded checks merged into
  `reports/latest-verification.json` (`2841` verified, `393` failed, `1548`
  skipped)
- Runtime evidence growth target: `22.5%` checked evidence is above the
  unrounded `10%` release target; `0` additional records are required for this
  target.
- Institution API overview: `411` organizations, `12060` APIs, and `21256`
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
- Sustainable coverage policy: `policy/sustainable-coverage.json`
- Sustainable coverage report: `reports/sustainable-coverage.json`
- Failure recovery policy: `policy/failure-recovery.json`
- Failure observations and rollup: `reports/failure-observations.json` and
  `reports/failure-recovery-rollup.json`

Recurring failures use stable identities across credential, parameter,
adapter, parser, rate-limit, upstream, reference-drift, catalog-drift, and
consumer classes. Transient observations remain retry evidence; repeated
observations become one owner-bound durable work item, so scheduled runs do not
create duplicate tickets. A healthy observation removes active work and emits a
recovery receipt. The generator also compares credential and Studio observations
with their current release evidence, preventing a ticket-only recovery from
leaving sustainable coverage stale.
- Registry data: `data/data-go-kr.registry.json`
- Provider index: `data/provider-index.json`
- Schema index: `schemas/index.json`
- Studio bundle schemas: `datapan.studio-datasets.v1`,
  `datapan.studio-bundle.v1`
- Catalog diff: `reports/catalog-diff.json`

`data/data-go-kr.registry.json` is tracked with Git LFS only as a source-tree
compatibility pointer. The canonical consumer distribution is the public
[`StatPan/datapan-registry` Hugging Face Dataset](https://huggingface.co/datasets/StatPan/datapan-registry),
and CI and release operators do not depend on Git LFS availability.
`scripts/materialize-canonical-registry.py`
downloads the public Hugging Face Dataset object at the immutable commit pinned
in `policy/registry-distribution.json`, then requires its bytes and SHA-256 to
match `manifest.json` before replacing the LFS pointer. Availability failures
exit with code `20`; manifest, policy, size, or checksum failures exit with code
`21`, so an unavailable mirror cannot be mistaken for corrupt registry bytes.
The normalized registry is larger than GitHub's normal blob limit.

Hugging Face publication uses a two-commit trust boundary. The first commit
contains the canonical Registry plus every manifest-bound release artifact and
the validated shard archive. The second commit publishes
`release/distribution-manifest.json`, which names the first immutable commit
and binds each path by byte size and SHA-256. This avoids a self-referential
commit hash while ensuring consumers never fetch Registry data from mutable
`main`. `scripts/huggingface_registry_distribution.py` stages, publishes, and
anonymously re-verifies that contract. The guarded `Publish Hugging Face
Registry distribution` workflow performs publication only when manually
dispatched with `publish=true` and `HF_TOKEN`; pull requests execute the same
staging and validation without credentials.

Upstream catalogue freshness is observed separately from publication. The
weekly `Upstream catalog refresh` workflow follows `policy/source-refresh.json`,
imports an ephemeral candidate, and emits schema-validated `no_change`,
`material_change`, or `collection_failure` evidence plus a review work packet.
It never publishes the candidate automatically.

## Verify

Coverage is intentionally layered. `operation_routable` describes static
registry routing, while `fresh_verified_operation` requires a successful,
timestamped runtime result within the policy freshness window. Missing source
catalog denominators and missing evidence timestamps count as uncovered. Check
the deterministic contract with:

```bash
python3 scripts/generate-sustainable-coverage.py --self-test
python3 scripts/generate-sustainable-coverage.py --check
```

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
readiness through `datapan-cli`, packages the current checkout as
`.datapan/release-assets/datapan-registry-snapshot.zip`, and installs that zip
with `datapan catalog install datapan-registry --url ...`. The current-zip
smoke writes `.datapan/ci/current-release-install.json`, then runs `datapan
doctor --json` against that installed registry and saves
`.datapan/ci/current-release-doctor.json` plus
`.datapan/ci/current-release-doctor-smoke.json`. The install and doctor JSON are
validated with repository smoke checks, including a byte and sha256 cross-check
that proves install wrote the canonical registry from the packaged zip. The
install check also writes `.datapan/ci/current-release-install-smoke.json` as
schema-validated release-health evidence before the doctor path and spec-count
cross-check summary proves doctor read the same registry. The
workflow also smoke-tests that the latest public GitHub Release zip can be
installed with `datapan catalog install datapan-registry`; that install smoke
writes
`.datapan/ci/latest-release-install.json` plus
schema-validated `.datapan/ci/latest-release-install-smoke.json`, recording
whether the release used validated shard metadata or the canonical monolith
fallback. A separate
`datapan doctor --json` run is saved as
`.datapan/ci/latest-release-doctor.json` plus
`.datapan/ci/latest-release-doctor-smoke.json` for the latest public release
install. After both current and latest install/doctor smoke summaries pass,
the workflow writes `.datapan/ci/release-health-rollup.json` as the
schema-validated top-level release-health verdict, proving the four smoke
summaries agree on provider, registry path, spec count, doctor install
cross-checks, and the current-vs-latest release zip evidence boundary. The
release manifest also includes `reports/release-consumer-compatibility.json`,
which records the canonical registry path, release-health evidence, optional
shard asset boundary, the release-health shard install fields that distinguish
validated shard metadata from canonical monolith fallback, and downstream
consumer compatibility expectations as a generated and schema-validated
artifact. That compatibility report also binds the manifest evidence contracts
for source contracts, source runtime evidence, error/action routing, downstream
impact, source reference drift, and source report inventory, so consumers can
see which checksum-verifiable release reports are required before treating the
registry as compatible.
The workflow also regenerates `reports/error-action-routing-rollup.json` from
checked-in source error-action catalogs so provider failure signatures stay
routed to bounded release, documentation, or adapter work as manifest-bound
evidence.
It also regenerates `reports/source-contract-rollup.json` from checked-in
`sources/*.json` profiles, binding provider contract status, auth, request,
response, runtime constraints, promotion state, and source profile checksums to
the release manifest.
The checked-in `reports/source-runtime-evidence-rollup.json` is also
manifest-bound, so release verification proves the source runtime evidence
plan inputs, blocker IDs, warning IDs, and zero-evidence source state that
`docs/source-runtime-readiness.md` presents to operators.
The release-wide `reports/registry-impact-plan.json` is manifest-bound as
well, preserving downstream action hints and no-action boundaries as
checksum-verifiable release evidence.
`reports/source-report-inventory.json` is also manifest-bound and records bytes
and sha256 for each listed source-scoped report, plus whether schema-backed
source reports are present in `schemas/index.json`, so nested source evidence
can be checked through the release ledger without adding every source report to
the top-level manifest.
Top-level schema-backed reports are guarded by
`scripts/validate-release-report-artifacts.py`, which requires release reports
to be manifest-bound with matching schema URI, bytes, and sha256 unless they
are explicit release verification/readiness receipts generated from the
manifest.
For checked-in non-schema manifest artifacts, run
`scripts/sync-release-manifest-artifacts.py --write` after regenerating reports
and `--check` in release gates so artifact bytes and sha256 stay deterministic.

The current snapshot was generated from `datapan-cli` and includes:

- normalized data.go.kr registry;
- provider index for registered adapters;
- schema index and versioned schemas;
- catalog diff against the previous published registry;
- catalog audit;
- error catalog;
- release-wide error/action routing rollup;
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
- source contracts are summarized as manifest-bound release evidence;
- source runtime evidence blockers and warnings are manifest-bound release
  evidence;
- downstream impact action hints are manifest-bound release evidence;
- top-level schema-backed release reports are either manifest-bound or explicit
  manifest-derived receipts;
- registry size and LFS handling are explicit;
- verification evidence is attached or documented;
- generated artifacts do not contain credentials.
- the `Verify registry release` workflow passes for the commit or tag.
- the scheduled release-health workflow continues passing between releases.

See `docs/release-cadence.md` for the repeatable release loop.
