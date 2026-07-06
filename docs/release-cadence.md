# Release Cadence

`datapan-registry` releases should be boring, repeatable, and evidence-first.

Release work should follow the standardization milestones in
`docs/registry-standardization-blueprint.md`; generated artifact updates should
identify which measured gap they reduce.
Release PRs should also satisfy `docs/registry-governance-policy.md`, including
warning handling and generated-artifact rules.

## Inputs

- A current `datapan-cli` build.
- A data.go.kr API key available to the CLI environment.
- The previous released registry when available, usually extracted under
  `.datapan/previous/` from the last GitHub Release asset.

## Draft

Generate a release draft from the `datapan-registry` checkout:

```bash
datapan catalog update data-go-kr --registry data/data-go-kr.registry.json --apply --backup --diff-limit 0 --json
datapan catalog release draft --registry data/data-go-kr.registry.json --previous-registry .datapan/previous/data-go-kr.registry.json --verification reports/latest-verification.json --output-dir . --provider-limit 0 --json
```

When there is no previous release yet, omit `--previous-registry`.

## Verify

Every release must pass:

```bash
datapan catalog release verify --manifest manifest.json --output reports/latest-release-verification.json --json
datapan catalog release readiness --manifest manifest.json --output reports/latest-release-readiness.json --json
```

## Guarded GitHub Draft

Maintainers may use the `Draft registry release` GitHub Actions workflow when
they want a repeatable, reviewable release draft without publishing anything.
The workflow is manually triggered with `workflow_dispatch` and has two modes:

- `verify-only`: verify the checked-in `manifest.json` and readiness reports.
- `draft-local`: generate a draft under `.datapan/draft/` from the checked-in
  registry and verification evidence, then verify that draft manifest.

The workflow uploads `.datapan/draft/` and `.datapan/ci/` as workflow artifacts.
When `include_shard_archive` is enabled, it also generates
`.datapan/release-assets/data-go-kr-shards.tar.gz` from the materialized
canonical registry, validates the shard inventory, checks the archive shape,
and verifies that the shard archive references the same canonical registry as
the generated `.datapan/release-assets/datapan-registry-snapshot.zip`. It does
not push commits, create tags, publish GitHub Releases, attach assets, or
capture provider credentials. Use local generation when updating upstream
catalog data or when provider API credentials are required. Use the guarded
GitHub draft when the registry artifact is already present and the release
operator wants Actions-based verification before committing, tagging, or
manually attaching release assets.

The repository also runs `.github/workflows/verify-release.yml` on pushes, pull
requests, manual dispatches, `v*` tags, and a weekly scheduled release-health
check. That workflow:

- checks out `datapan-registry` with Git LFS enabled;
- checks that `data/data-go-kr.registry.json` is the full materialized file,
  not an LFS pointer;
- checks out `StatPan/datapan-cli`;
- rereads provider-specific verification reports and regenerates bounded
  summaries for qnet, epost, ekape, emuseum, forest, folk, garak, airport,
  andong, culture, data-gg, happysd, i815, jeonju, gblib, geoje, gwanak,
  humetro, itfind, korad, kpx, lh-ebid, mafra, myhome, naqs, ncpms, nfqs, nongsaro,
  oneclick-law, pqis, seoul-bus, seoul-open-data, sisul, tour, uiryeong,
  ulsan, work24, and the merged latest report;
- regenerates and validates `reports/error-action-routing-rollup.json` from
  checked-in source error-action catalogs so provider failures remain routed to
  bounded release, documentation, or adapter work;
- runs `catalog release verify`;
- runs `catalog release readiness`;
- packages the current checkout as
  `.datapan/release-assets/datapan-registry-snapshot.zip`, checks the zip
  inventory and checksums, serves it locally, and installs it with
  `datapan catalog install datapan-registry --url ...`;
- checks that the installed current checkout registry has the same byte count
  and sha256 as the canonical registry artifact inside the packaged zip;
- runs `datapan doctor --json` against the installed current checkout registry;
- checks that the README Current Snapshot matches the generated coverage,
  provider-index, and verification-summary artifacts;
- installs the latest GitHub Release zip with
  `datapan catalog install datapan-registry`;
- validates the install JSON with
  `scripts/check-shard-aware-install-smoke.py` for both the current checkout
  zip and the latest public release, recording schema-validated evidence for
  either validated shard metadata or canonical monolith fallback in
  `current-release-install-smoke.json` and `latest-release-install-smoke.json`;
- validates the doctor JSON with `scripts/check-release-doctor-smoke.py` for
  both current checkout and latest public release installs, including matching
  doctor registry path and spec count against the install JSON in
  `current-release-doctor-smoke.json` and `latest-release-doctor-smoke.json`;
- rolls those four install/doctor smoke summaries into
  `release-health-rollup.json`, a schema-validated top-level verdict that
  checks provider, registry path, spec count, doctor install cross-check, and
  current-vs-latest release zip evidence invariants;
- regenerates and validates `reports/release-consumer-compatibility.json`, the
  manifest-bound downstream compatibility matrix that keeps the canonical
  registry path required, release-health evidence named, shard install fields
  tied to the rollup, and shard assets optional until shard-preferred monolith
  fallback is proven downstream. The report also names the required manifest
  evidence contracts for source contracts, source runtime evidence,
  error/action routing, downstream impact, source reference drift, and source
  report inventory, and validation checks their bytes and sha256 values against
  `manifest.json`;
- regenerates and validates `reports/source-contract-rollup.json`, the
  manifest-bound upstream contract rollup that records checked-in source
  profile checksums plus provider status, auth, request, response, runtime, and
  promotion constraints;
- validates `reports/source-runtime-evidence-rollup.json` and its generated
  `docs/source-runtime-readiness.md` view, with the rollup manifest-bound so
  source runtime plan inputs, blockers, warnings, and zero-evidence sources are
  checksum-verifiable release evidence;
- regenerates and validates `reports/registry-impact-plan.json`, the
  manifest-bound downstream impact rollup that preserves client/server action
  hints, manual-review flags, and no-action boundaries for consumers;
- validates top-level schema-backed report manifest coverage with
  `scripts/validate-release-report-artifacts.py`, so new release reports cannot
  stay outside `manifest.json` unless they are explicit manifest-derived
  verification/readiness receipts;
- uploads current-checkout install and doctor JSON plus latest-public-release
  install and doctor JSON reports as release-health evidence.

Recommended evidence before tagging:

```bash
datapan catalog verify --registry data/data-go-kr.registry.json --limit 100 --output reports/latest-verification.json --json
datapan catalog verify summary --input reports/latest-verification.json --output reports/latest-verification-summary.json --json
python scripts/generate-coverage-backlog.py
python scripts/generate-external-adapter-backlog.py
python scripts/generate-operation-materialization-plan.py
python scripts/generate-safetydata-operation-candidates.py
python scripts/generate-safetydata-registry-patches.py
python scripts/generate-institution-api-overview.py
python scripts/generate-institution-runtime-plan.py
python scripts/generate-source-report-inventory.py
python scripts/generate-source-contract-rollup.py
python scripts/generate-source-runtime-readiness.py
python scripts/generate-error-action-routing-rollup.py
```

Institution-scoped runtime reactivation batches should follow the priority
order in `docs/data-go-kr-coverage-backlog.md` and
`docs/data-go-kr-institution-api-overview.md`. Start with the largest
institution/runtime gap, run a bounded batch with `--org`, merge the batch into
`reports/latest-verification.json`, then regenerate the backlog and overview:

```bash
datapan catalog verify --registry data/data-go-kr.registry.json --org 행정안전부 --kind data_go_kr_gateway --limit 100 --timeout 20s --output reports/data-go-kr/mois-verification.json --json
datapan catalog verify merge --input reports/latest-verification.json --input reports/data-go-kr/mois-verification.json --output reports/latest-verification.json --json
datapan catalog verify summary --input reports/latest-verification.json --output reports/latest-verification-summary.json --json
python scripts/generate-coverage-backlog.py
python scripts/generate-operation-materialization-plan.py
python scripts/generate-institution-api-overview.py
python scripts/generate-institution-runtime-plan.py
python scripts/validate-coverage-backlog.py
python scripts/validate-operation-materialization-plan.py
python scripts/validate-institution-api-overview.py
python scripts/validate-institution-runtime-plan.py
```

The first queue is `행정안전부`: the current backlog lists `1252` APIs,
`618` APIs with operation mappings, `634` uncovered APIs, and `618` runtime
reactivation APIs. Gateway verification requires a data.go.kr service key in
the local or CI environment; without it, the same command can only produce
`missing_auth` boundary evidence and should not replace live runtime evidence.

The `Institution runtime verification` workflow can run the same batch from
GitHub Actions with repository secrets. Dispatch it with `organization`,
`kind`, `limit`, and `timeout` inputs; it defaults to `행정안전부`,
`data_go_kr_gateway`, `100`, and `20s`. The workflow requires either the
`DATAPAN_DATA_GO_KR_KEY`, `DATA_PORTAL_API_KEY`, or
`DATA_GO_KR_SERVICE_KEY` secret, excludes `reports/latest-verification.json`,
and uploads the verification report, summary, command, stdout, exit code, and
non-secret metadata as an artifact. After inspecting the artifact, merge the
completed report into `reports/latest-verification.json` with the commands
above and regenerate the derived reports.

Operation materialization should follow
`docs/data-go-kr-operation-materialization-plan.md`. That plan is generated
from `reports/data-go-kr/coverage-backlog.json` and keeps APIs without
operation mappings separate from runtime reactivation work. Start with the
largest institution surface, materialize the planned API metadata into registry
operation mappings, then regenerate the coverage backlog, operation
materialization plan, institution API overview, and institution runtime plan.
For Safety Data linked APIs, refresh operation candidates with:

```bash
python scripts/generate-safetydata-operation-candidates.py --batch reports/data-go-kr/operation-materialization-batches/institution-01.json --limit 10
python scripts/validate-safetydata-operation-candidates.py
python scripts/generate-safetydata-registry-patches.py
python scripts/validate-safetydata-registry-patches.py
```

The `Safety Data operation discovery` workflow runs the same discovery without
credentials and uploads the candidate report as an artifact.

Provider-specific evidence should be accumulated for registered external
adapters:

```bash
datapan catalog verify --registry data/data-go-kr.registry.json --provider q-net --kind external_endpoint --limit 5 --output reports/qnet-verification.json --json
datapan catalog verify summary --input reports/qnet-verification.json --output reports/qnet-verification-summary.json --json
datapan catalog verify --registry data/data-go-kr.registry.json --provider epost --kind external_endpoint --limit 5 --output reports/epost-verification.json --json
datapan catalog verify summary --input reports/epost-verification.json --output reports/epost-verification-summary.json --json
datapan catalog verify --registry data/data-go-kr.registry.json --provider ekape --kind external_endpoint --limit 5 --output reports/ekape-verification.json --json
datapan catalog verify summary --input reports/ekape-verification.json --output reports/ekape-verification-summary.json --json
datapan catalog verify --registry data/data-go-kr.registry.json --provider emuseum --kind external_endpoint --limit 3 --timeout 20s --output reports/emuseum-verification.json --json
datapan catalog verify summary --input reports/emuseum-verification.json --output reports/emuseum-verification-summary.json --json
datapan catalog verify --registry data/data-go-kr.registry.json --provider forest --kind external_endpoint --limit 4 --output reports/forest-verification.json --json
datapan catalog verify summary --input reports/forest-verification.json --output reports/forest-verification-summary.json --json
datapan catalog verify --registry data/data-go-kr.registry.json --provider folk --kind external_endpoint --limit 3 --output reports/folk-verification.json --json
datapan catalog verify summary --input reports/folk-verification.json --output reports/folk-verification-summary.json --json
datapan catalog verify --registry data/data-go-kr.registry.json --provider garak --kind external_endpoint --limit 3 --timeout 20s --output reports/garak-verification.json --json
datapan catalog verify summary --input reports/garak-verification.json --output reports/garak-verification-summary.json --json
datapan catalog verify --registry data/data-go-kr.registry.json --provider gblib --kind external_endpoint --limit 3 --output reports/gblib-verification.json --json
datapan catalog verify summary --input reports/gblib-verification.json --output reports/gblib-verification-summary.json --json
datapan catalog verify --registry data/data-go-kr.registry.json --provider airport --kind external_endpoint --limit 6 --output reports/airport-verification.json --json
datapan catalog verify summary --input reports/airport-verification.json --output reports/airport-verification-summary.json --json
datapan catalog verify --registry data/data-go-kr.registry.json --provider andong --kind external_endpoint --limit 15 --output reports/andong-verification.json --json
datapan catalog verify summary --input reports/andong-verification.json --output reports/andong-verification-summary.json --json
datapan catalog verify --registry data/data-go-kr.registry.json --provider culture --kind external_endpoint --limit 5 --timeout 20s --output reports/culture-verification.json --json
datapan catalog verify summary --input reports/culture-verification.json --output reports/culture-verification-summary.json --json
datapan catalog verify --registry data/data-go-kr.registry.json --provider data-gg --kind external_endpoint --limit 18 --timeout 20s --output reports/data-gg-verification.json --json
datapan catalog verify summary --input reports/data-gg-verification.json --output reports/data-gg-verification-summary.json --json
datapan catalog verify --registry data/data-go-kr.registry.json --provider happysd --kind external_endpoint --limit 5 --timeout 20s --output reports/happysd-verification.json --json
datapan catalog verify summary --input reports/happysd-verification.json --output reports/happysd-verification-summary.json --json
datapan catalog verify --registry data/data-go-kr.registry.json --provider i815 --kind external_endpoint --limit 5 --timeout 20s --output reports/i815-verification.json --json
datapan catalog verify summary --input reports/i815-verification.json --output reports/i815-verification-summary.json --json
datapan catalog verify --registry data/data-go-kr.registry.json --provider jeonju --kind external_endpoint --limit 5 --output reports/jeonju-verification.json --json
datapan catalog verify summary --input reports/jeonju-verification.json --output reports/jeonju-verification-summary.json --json
datapan catalog verify --registry data/data-go-kr.registry.json --provider geoje --kind external_endpoint --limit 6 --output reports/geoje-verification.json --json
datapan catalog verify summary --input reports/geoje-verification.json --output reports/geoje-verification-summary.json --json
datapan catalog verify --registry data/data-go-kr.registry.json --provider gwanak --kind external_endpoint --limit 3 --timeout 20s --output reports/gwanak-verification.json --json
datapan catalog verify summary --input reports/gwanak-verification.json --output reports/gwanak-verification-summary.json --json
datapan catalog verify --registry data/data-go-kr.registry.json --provider humetro --kind external_endpoint --limit 8 --output reports/humetro-verification.json --json
datapan catalog verify summary --input reports/humetro-verification.json --output reports/humetro-verification-summary.json --json
datapan catalog verify --registry data/data-go-kr.registry.json --provider itfind --kind external_endpoint --limit 13 --output reports/itfind-verification.json --json
datapan catalog verify summary --input reports/itfind-verification.json --output reports/itfind-verification-summary.json --json
datapan catalog verify --registry data/data-go-kr.registry.json --provider korad --kind external_endpoint --limit 15 --output reports/korad-verification.json --json
datapan catalog verify summary --input reports/korad-verification.json --output reports/korad-verification-summary.json --json
datapan catalog verify --registry data/data-go-kr.registry.json --provider kpx --kind external_endpoint --limit 6 --timeout 20s --output reports/kpx-verification.json --json
datapan catalog verify summary --input reports/kpx-verification.json --output reports/kpx-verification-summary.json --json
datapan catalog verify --registry data/data-go-kr.registry.json --provider lh-ebid --kind external_endpoint --limit 6 --output reports/lh-ebid-verification.json --json
datapan catalog verify summary --input reports/lh-ebid-verification.json --output reports/lh-ebid-verification-summary.json --json
datapan catalog verify --registry data/data-go-kr.registry.json --provider mafra --kind external_endpoint --limit 3 --timeout 20s --output reports/mafra-verification.json --json
datapan catalog verify summary --input reports/mafra-verification.json --output reports/mafra-verification-summary.json --json
datapan catalog verify --registry data/data-go-kr.registry.json --provider myhome --kind external_endpoint --limit 1 --timeout 20s --output reports/myhome-verification.json --json
datapan catalog verify summary --input reports/myhome-verification.json --output reports/myhome-verification-summary.json --json
datapan catalog verify --registry data/data-go-kr.registry.json --provider naqs --kind external_endpoint --limit 9 --output reports/naqs-verification.json --json
datapan catalog verify summary --input reports/naqs-verification.json --output reports/naqs-verification-summary.json --json
datapan catalog verify --registry data/data-go-kr.registry.json --provider ncpms --kind external_endpoint --limit 5 --timeout 20s --output reports/ncpms-verification.json --json
datapan catalog verify summary --input reports/ncpms-verification.json --output reports/ncpms-verification-summary.json --json
datapan catalog verify --registry data/data-go-kr.registry.json --provider nfqs --kind external_endpoint --limit 5 --timeout 20s --output reports/nfqs-verification.json --json
datapan catalog verify summary --input reports/nfqs-verification.json --output reports/nfqs-verification-summary.json --json
datapan catalog verify --registry data/data-go-kr.registry.json --provider nongsaro --kind external_endpoint --limit 4 --timeout 20s --output reports/nongsaro-verification.json --json
datapan catalog verify summary --input reports/nongsaro-verification.json --output reports/nongsaro-verification-summary.json --json
datapan catalog verify --registry data/data-go-kr.registry.json --provider oneclick-law --kind external_endpoint --limit 30 --output reports/oneclick-law-verification.json --json
datapan catalog verify summary --input reports/oneclick-law-verification.json --output reports/oneclick-law-verification-summary.json --json
datapan catalog verify --registry data/data-go-kr.registry.json --provider pqis --kind external_endpoint --limit 4 --timeout 15s --output reports/pqis-verification.json --json
datapan catalog verify summary --input reports/pqis-verification.json --output reports/pqis-verification-summary.json --json
datapan catalog verify --registry data/data-go-kr.registry.json --provider seoul-bus --kind external_endpoint --limit 5 --output reports/seoul-bus-verification.json --json
datapan catalog verify summary --input reports/seoul-bus-verification.json --output reports/seoul-bus-verification-summary.json --json
datapan catalog verify --registry data/data-go-kr.registry.json --provider seoul-open-data --kind external_endpoint --limit 5 --timeout 20s --output reports/seoul-open-data-verification.json --json
datapan catalog verify summary --input reports/seoul-open-data-verification.json --output reports/seoul-open-data-verification-summary.json --json
datapan catalog verify --registry data/data-go-kr.registry.json --provider sisul --kind external_endpoint --limit 20 --timeout 4s --output reports/sisul-verification.json --json
datapan catalog verify summary --input reports/sisul-verification.json --output reports/sisul-verification-summary.json --json
datapan catalog verify --registry data/data-go-kr.registry.json --provider tour --kind external_endpoint --limit 7 --output reports/tour-external-verification.json --json
datapan catalog verify --registry data/data-go-kr.registry.json --provider tour --kind service_root --limit 26 --output reports/tour-service-root-verification.json --json
datapan catalog verify merge --input reports/tour-external-verification.json --input reports/tour-service-root-verification.json --output reports/tour-verification.json --json
datapan catalog verify summary --input reports/tour-verification.json --output reports/tour-verification-summary.json --json
datapan catalog verify --registry data/data-go-kr.registry.json --provider uiryeong --kind external_endpoint --limit 6 --output reports/uiryeong-verification.json --json
datapan catalog verify summary --input reports/uiryeong-verification.json --output reports/uiryeong-verification-summary.json --json
datapan catalog verify --registry data/data-go-kr.registry.json --provider ulsan --kind external_endpoint --limit 6 --output reports/ulsan-verification.json --json
datapan catalog verify summary --input reports/ulsan-verification.json --output reports/ulsan-verification-summary.json --json
datapan catalog verify --registry data/data-go-kr.registry.json --provider work24 --kind external_endpoint --limit 3 --timeout 20s --output reports/work24-verification.json --json
datapan catalog verify summary --input reports/work24-verification.json --output reports/work24-verification-summary.json --json
datapan catalog verify merge --input reports/qnet-verification.json --input reports/epost-verification.json --input reports/ekape-verification.json --input reports/emuseum-verification.json --input reports/forest-verification.json --input reports/folk-verification.json --input reports/garak-verification.json --input reports/gblib-verification.json --input reports/airport-verification.json --input reports/andong-verification.json --input reports/culture-verification.json --input reports/data-gg-verification.json --input reports/happysd-verification.json --input reports/i815-verification.json --input reports/jeonju-verification.json --input reports/geoje-verification.json --input reports/gwanak-verification.json --input reports/humetro-verification.json --input reports/itfind-verification.json --input reports/korad-verification.json --input reports/kpx-verification.json --input reports/lh-ebid-verification.json --input reports/mafra-verification.json --input reports/myhome-verification.json --input reports/naqs-verification.json --input reports/ncpms-verification.json --input reports/nfqs-verification.json --input reports/nongsaro-verification.json --input reports/oneclick-law-verification.json --input reports/pqis-verification.json --input reports/seoul-bus-verification.json --input reports/seoul-open-data-verification.json --input reports/sisul-verification.json --input reports/tour-verification.json --input reports/uiryeong-verification.json --input reports/ulsan-verification.json --input reports/work24-verification.json --output reports/latest-verification.json --json
datapan catalog verify summary --input reports/latest-verification.json --output reports/latest-verification-summary.json --json
```

The merged report is the release evidence artifact. Provider-specific reports
may stay in `reports/` as supporting evidence, while
`reports/latest-verification.json` and
`reports/latest-verification-summary.json` are included in `manifest.json`.
Skipped and failed results are kept because they explain current provider
boundaries, such as WADL-only metadata, unsupported SOAP operations, separate
key registration requirements, or upstream provider HTML responses.

## Publish

1. Commit generated artifacts.
   Update README Current Snapshot from `reports/coverage.json`,
   `data/provider-index.json`, and `reports/latest-verification-summary.json`
   in the same commit.
2. Tag with `vYYYY.MM.DD`, or `vYYYY.MM.DD.N` for a second release on the same
   date.
3. Push the branch and tag.
4. Create a GitHub Release.
5. Attach the `datapan-registry-snapshot.zip` archive produced by the
   `Draft registry release` workflow, renaming it to the tag-specific release
   asset name if needed, so users can consume the snapshot without relying on
   Git LFS.
6. If shard publication is desired, attach the
   `data-go-kr-shards.tar.gz` archive from the same `Draft registry release`
   run after confirming `release-asset-consistency-check.txt` passed.
7. Confirm the `Verify registry release` workflow passes on the tag.

Shard-aware registry artifacts, when generated, must remain additive during the
compatibility period described in
`docs/registry-shard-artifact-strategy.md`. Full release verification continues
to require the canonical LFS registry until the release verifier, install path,
doctor checks, and downstream consumers explicitly support shard-preferred
fallback behavior.

The `Registry shard validation` workflow is intentionally separate from the
full release gate. It checks shard generator and validator behavior against a
small fixture without Git LFS, credentials, release install, or doctor checks.
It does not prove release readiness. The `Verify registry release` workflow
remains the full LFS-backed release and install gate.

Shard files should not be committed as ordinary Git blobs. The first
publication mode is a compressed GitHub Release asset named
`data-go-kr-shards.tar.gz`, generated from `data/data-go-kr.registry.json` and
validated with `scripts/validate-registry-shards.py`. Keep the archive optional
until downstream consumers support shard-preferred, monolith-fallback
installation and verification.

Do not make a shard archive required for install, doctor, release verification,
or readiness until `datapan-cli` proves fallback behavior for the commands
listed in `docs/registry-shard-artifact-strategy.md`.

## Cadence

Start with date-based releases and keep a weekly scheduled health check running
between releases. The scheduled workflow does not publish a new release by
itself. It proves that the checked-in manifest remains verifiable and that the
latest public release asset remains installable.

Move from scheduled health checks to scheduled release drafting only after:

- the import command is stable across repeated full catalog pulls;
- release verification and readiness reports are consistently useful;
- provider adapter evidence is improving across releases;
- consumers can pin either a git tag or a release asset.

## Non-Goals

- Do not store credentials.
- Do not edit generated JSON artifacts by hand.
- Do not claim every API is callable merely because it appears in the catalog.
- Do not remove failed or skipped verification evidence just to make the release
  look cleaner.
