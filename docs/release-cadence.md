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
python scripts/refresh-release-ledger-evidence.py --write
python scripts/refresh-release-ledger-evidence.py --check
python scripts/generate-release-assembly-receipt.py
python scripts/generate-release-assembly-receipt.py --check
datapan catalog release verify --manifest manifest.json --output reports/latest-release-verification.json --json
datapan catalog release readiness --manifest manifest.json --output reports/latest-release-readiness.json --json
python scripts/validate-release-receipt-boundary.py
```

`scripts/refresh-release-ledger-evidence.py --write` is the default local
refresh command for checked-in release-ledger evidence. It reruns the existing
schema, manifest, source, credential-boundary, impact, compatibility, goal, and
assembly generators until their check commands converge or a bounded iteration
limit fails with the stale command that still needs attention. The command is
secret-free: it does not run credentialed collection and does not check in local
runtime session or review-plan handoff files.

`reports/release-assembly-receipt.json` is the operator-facing assembly receipt
for the current checkout. It binds the release assembly phases for canonical
registry materialization, schema sync, manifest digest sync, source contracts,
source runtime evidence, source runtime remediation mapping, error/action
routing, downstream impact, consumer compatibility, verification/readiness
receipts, shard archive evidence, release zip packaging, and goal completion
audit checks. The `--check` mode validates
the receipt against `schemas/datapan.release-assembly-receipt.v1.schema.json`
and fails if the receipt drifts from the `Verify registry release` or
`Draft registry release` workflow command fragments.

`reports/latest-release-verification.json` and
`reports/latest-release-readiness.json` are manifest-derived receipts. Refresh
them from the selected `manifest.json` with the two `datapan catalog release`
commands above, after manifest-bound release evidence has been regenerated and
`manifest.json` has been updated. They are not listed in `manifest.json`
because they are receipts produced by validating that manifest, not independent
source artifacts that the manifest must checksum. CI writes the current-checkout
or selected-manifest equivalents under `.datapan/ci/` so workflow evidence can
verify the same command path without rewriting the checked-in receipt files.

## Immutable Receipt Admission Foundation

`schemas/datapan.release-receipt-admission.v1.schema.json` defines the
Registry-side, offline admission boundary for immutable producer receipts. A
receipt is bound to a producer commit and producer-receipt digest plus the
selected Registry manifest, source-registry, and admission-policy digests. Its
`receipt_digest` is SHA-256 over canonical JSON with that field omitted, so it
does not self-reference. The separately supplied producer artifact root is
required at admission: aggregate and shard files are resolved below that root,
byte-checked against declared SHA-256 values, and cannot escape via path
traversal or symlinks. The explicit admission time applies producer-kind
freshness limits and rejects future receipts.

Validate local contract fixtures without a provider call, Registry publication,
or Datapan CLI checkout:

```bash
python3 scripts/validate-release-admission-receipts.py \
  --manifest tests/fixtures/release-admission/manifest.json \
  --check-manifest-artifacts \
  --admission-time 2026-07-22T01:00:00Z \
  --producer-artifact-root StatPan/datapan-data=tests/fixtures/release-admission/producer-artifacts/datapan-data \
  --producer-artifact-root StatPan/datapan-health=tests/fixtures/release-admission/producer-artifacts/datapan-health \
  --producer-artifact-root StatPan/datapan-cli=tests/fixtures/release-admission/producer-artifacts/datapan-cli \
  tests/fixtures/release-admission/catalog-observation.json \
  tests/fixtures/release-admission/cli-consumer-smoke.json \
  tests/fixtures/release-admission/runtime-freshness-shard-0.json
```

The `runtime_freshness_shard` kind is exclusively for the rotating eight-shard
freshness run. It requires one immutable producer revision, one Registry
manifest/source/policy binding, all shard indexes 0 through 7, batch size at
most 100, parallelism at most 2, and per-operation timeout at most 20 seconds.
Its Health aggregate and selected shard artifact are byte-bound to the outer
Registry envelope. A partial aggregate (`receipt_available=false` for any
shard) cannot synthesize an outer receipt and fails completeness. It does not
admit or replace Health canary observations. This is a contract foundation
only; the current external-checkout guard remains in force until later
producer, workflow-cutover, and rollback evidence is proven.

`health_live_observation` is a separate pre-publication release input. It
binds one complete, redacted Health bounded-observation aggregate as both the
primary producer artifact and aggregate artifact, and is fresh for at most 600
seconds at the caller-owned admission time. Admission independently checks the
aggregate completion and every complete shard's `observed_at` against that same
clock; a freshly stamped outer envelope cannot rewrap stale or future Health
evidence. It retains the same eight-shard
bounded execution limits but is not interchangeable with a
`runtime_freshness_shard`; a cutover caller must explicitly require this kind.
No receipt of either kind authorizes publication by itself.

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

## Canonical Hugging Face Distribution

GitHub source and Release assets remain explicit compatibility surfaces, but
the canonical large Registry distribution is
`StatPan/datapan-registry` on Hugging Face. Publication is two phase:

1. Materialize and validate the Registry against `manifest.json`.
2. Stage every manifest-bound artifact plus the validated shard inventory and
   archive.
3. Upload that payload without Git or Git LFS and retain the returned commit
   SHA.
4. Generate `release/distribution-manifest.json` naming that immutable payload
   commit and binding every path by bytes and SHA-256.
5. Upload the pointer manifest in a second commit.
6. Clear `HF_TOKEN` and anonymously download every artifact from the payload
   commit to verify the public receipt.

The manual `Publish Hugging Face Registry distribution` workflow implements
this sequence. A missing token stops before publication. A missing artifact,
mutable or malformed revision, byte mismatch, checksum mismatch, upload
failure, or anonymous verification failure prevents a successful publication
receipt. Registry readiness and goal completion must not treat a staged-only
run as public distribution evidence.

The repository also runs `.github/workflows/verify-release.yml` on pushes, pull
requests, manual dispatches, `v*` tags, and a weekly scheduled release-health
check. That workflow:

- checks out `datapan-registry` without fetching Git LFS and materializes the
  manifest-bound canonical registry from the immutable public mirror;
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

## Scheduled upstream observation

`.github/workflows/credential-runtime-collection.yml` runs a separate bounded
credential observation every Wednesday at 19:17 UTC (Thursday 04:17 KST).
Scheduled events resolve explicitly to `collect`; manual dispatch keeps the
`preflight`/`collect` choice. The run collects only sources whose repository
secret is present, skips missing sources such as Seoul without suppressing the
other receipts, and uploads staged redacted evidence for 14 days. It never
promotes a receipt or writes reviewed evidence; promotion remains an explicit
human-review operation. These observations are the recurring input to the
freshness and recovery cycle in #483.

`.github/workflows/upstream-catalog-refresh.yml` runs the operation-denominator
source importer on the cadence declared in `policy/source-refresh.json`. It
always operates as a dry run: the current manifest-bound registry is the
baseline, the imported registry is an ephemeral candidate, and no tag, release,
registry commit, or public asset is written. Successful observations produce a
schema-validated deterministic full diff and classify the result as
`no_change` or `material_change`. Import, credential, and upstream failures
produce `collection_failure` evidence with no diff summary, so a failed
observation can never masquerade as zero drift.

Every run uploads the candidate snapshot when available, catalog diff,
`upstream-refresh-evidence.json`, and a stable-key
`upstream-refresh-work-packet.json`. Material drift is routed to human review;
publication remains false until release manifest verification, readiness, and
consumer compatibility gates run through the existing release workflow.
- regenerates and validates `reports/release-consumer-compatibility.json`, the
  manifest-bound downstream compatibility matrix that keeps the canonical
  registry path required, release-health evidence named, shard install fields
  tied to the rollup, and shard assets optional until shard-preferred monolith
  fallback is proven downstream. The report also names the required manifest
  evidence contracts for source contracts, source runtime evidence, source
  runtime remediation, error/action routing, downstream impact, source reference
  drift, and source report inventory, and validation checks their bytes and
  sha256 values against `manifest.json`;
- regenerates and validates `reports/source-contract-rollup.json`, the
  manifest-bound upstream contract rollup that records checked-in source
  profile checksums plus provider status, auth, request, response, runtime, and
  promotion constraints;
- validates `reports/source-runtime-evidence-rollup.json` and its generated
  `docs/source-runtime-readiness.md` view, with the rollup manifest-bound so
  source runtime plan inputs, blockers, warnings, and zero-evidence sources are
  checksum-verifiable release evidence;
- regenerates and validates `reports/source-runtime-remediation-map.json`, the
  manifest-bound source-by-source map that routes runtime blockers and warnings
  to resolved status, manual-review release boundaries, or follow-up work before
  consumer compatibility can consume the risk state;
- validates `reports/source-report-inventory.json`, the manifest-bound
  inventory of source-scoped reports whose entries carry bytes and sha256 so
  nested source evidence can be checked without listing every source report in
  the top-level manifest. The inventory also reports which schema-backed source
  reports are indexed in `schemas/index.json` and which still need schema
  promotion work;
- regenerates and validates `reports/registry-impact-plan.json`, the
  manifest-bound downstream impact rollup that preserves client/server action
  hints, manual-review flags, and no-action boundaries for consumers. The
  rollup also records release evidence inputs for error/action routing, source
  report inventory, and the validator checks the consumer compatibility
  downstream impact contract so impact state cannot drift away from the evidence
  consumers use;
- validates top-level schema-backed report manifest coverage with
  `scripts/validate-release-report-artifacts.py`, so new release reports cannot
  stay outside `manifest.json` or carry stale schema, bytes, or sha256 metadata
  unless they are explicit manifest-derived verification/readiness receipts;
- checks non-schema manifest artifact bytes and sha256 with
  `scripts/sync-release-manifest-artifacts.py --check`, keeping checked-in
  report digests reproducible instead of hand-maintained during release
  assembly;
- validates `docs/release-ledger-ownership.json` with
  `scripts/validate-release-ledger-ownership.py`, so every manifest artifact
  kind has an explicit release owner, generator/check path, package
  relationship, schema relationship, and exemption boundary;
- uploads current-checkout install and doctor JSON plus latest-public-release
  install and doctor JSON reports as release-health evidence.

The release-health rollup is generated only after all four smoke summaries are
present and schema-valid:

```bash
python scripts/generate-release-health-rollup.py \
  --current-install .datapan/ci/current-release-install-smoke.json \
  --current-doctor .datapan/ci/current-release-doctor-smoke.json \
  --latest-install .datapan/ci/latest-release-install-smoke.json \
  --latest-doctor .datapan/ci/latest-release-doctor-smoke.json \
  --output .datapan/ci/release-health-rollup.json
python scripts/validate-release-health-rollups.py \
  --schema schemas/datapan.release-health-rollup.v1.schema.json \
  .datapan/ci/release-health-rollup.json
```

`reports/release-consumer-compatibility.json` records that same rollup generation
contract under `release_health_evidence.rollup_generation_contract`, and
`scripts/validate-release-consumer-compatibility.py` checks the report, schema,
source runtime remediation map, and `.github/workflows/verify-release.yml` stay
aligned with those inputs.

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
python scripts/sync-release-schema-artifacts.py --write
python scripts/sync-release-manifest-artifacts.py --write
python scripts/validate-release-ledger-ownership.py
```

Scheduled runtime-freshness artifacts must cross the sanitized receipt
boundary before they can update checked-in evidence. Preview the exact status
delta first; the command verifies the receipt byte count and SHA-256, rejects
request/credential-shaped fields, and skips results already present verbatim:

```bash
python3 scripts/import-runtime-freshness-run.py \
  --report /path/to/consolidated/verification.json \
  --receipt /path/to/consolidated/run-receipt.json \
  --datapan-command datapan \
  --dry-run
```

After reviewing that bounded proposal, replace `--dry-run` with `--apply`.
Apply mode writes `reports/latest-verification.json` and the untruncated
`reports/latest-verification-summary.json` only after sanitization, digest,
count reconciliation, merge, and summary generation all succeed. Regenerate
runtime-evidence growth, freshness/recovery outputs, README snapshot, and the
release-ledger fixed point in the same ticket before publishing. Re-running an
already imported artifact is a zero-delta operation.

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
When the shard archive is attached to a release draft, the release package check
must run with `--shard-archive` so `package-registry-release.py` proves the
archive's `source_registry_sha256` matches the canonical registry artifact in
the selected release manifest.

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
