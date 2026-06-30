# Registry Standardization Blueprint

`datapan-registry` should become the public data standardization ledger for
Datapan. The repository should make source contracts, coverage, evidence,
errors, and downstream impact explicit enough that data work can be reviewed,
released, and monitored without relying on tribal knowledge.

This blueprint is the planning layer above the individual schemas and reports.
Every registry PR should be able to explain which gap in this document it
reduces.

Use `docs/registry-governance-policy.md` as the policy layer for gap statements,
quality gates, naming, warning handling, and client/server integration
boundaries. A milestone is not complete if it reaches the artifact checklist
while violating that policy.

## North Star

For every supported public data source, Datapan should be able to answer:

- Where is the official source documentation and when was it reviewed?
- Which APIs, datasets, operations, and endpoints are part of the published
  registry?
- Which operations are callable, blocked, deprecated, approval-required, or
  unsupported?
- Which source-specific behaviors affect auth, pagination, response parsing,
  rate limits, and error handling?
- Which runtime checks prove the source still behaves as expected?
- Which downstream systems need action when the registry changes?

The registry is successful when public data changes become measurable release
events instead of ad hoc debugging sessions.

## Target Architecture

The target registry has five connected layers:

1. Source inventory
   - source identity, official references, review timestamps, source family.
   - target artifact: `sources/<source_id>.json`.
   - schema: `datapan.source-profile.v1`.

2. Normalized registry artifacts
   - source-specific registry files under `data/`.
   - compatibility path for `data/data-go-kr.registry.json`.
   - release-wide `manifest.json`.

3. Evidence reports
   - coverage, audit, dependencies, route disposition, verification, and
     readiness.
   - source-scoped reports under `reports/<source>/` plus release-wide rollups.

4. Error and action routing
   - error field inventory from the registry.
   - source-specific error action catalog rules.
   - impact plan action hints for downstream repositories.

5. Gates and automation
   - release verification and readiness.
   - scheduled health checks.
   - source reference drift checks.
   - provider/runtime verification matrix.
   - downstream impact plan generation.

## Current State

Current strengths:

- `manifest.json` provides artifact checksums for the current release.
- `schemas/` contains versioned JSON contracts for existing release reports.
- `sources/data_go_kr.json` validates against `datapan.source-profile.v1`.
- `sources/kosis.json`, `sources/ecos.json`, `sources/open_assembly.json`,
  and `sources/seoul_open_data.json` validate against
  `datapan.source-profile.v1`.
- `reports/data-go-kr/error-action-catalog.json` validates against
  `datapan.error-action-catalog.v1`.
- `reports/kosis/error-action-catalog.json`,
  `reports/ecos/error-action-catalog.json`,
  `reports/open-assembly/error-action-catalog.json`, and
  `reports/seoul-open-data/error-action-catalog.json` provide draft
  source-specific action routing for the first non-data.go.kr profile batch.
- `reports/data-go-kr/external-coverage-summary.json` separates raw external
  adapter coverage from evidence-adjusted adapter candidates.
- `reports/data-go-kr/registry-impact-plan.json` validates downstream action
  hints for the current data.go.kr registry-only changes.
- `reports/registry-impact-plan.json` is generated from checked-in
  source-scoped impact plans and validates as the release-wide client/server
  action rollup.
- `reports/source-reference-drift.json` validates a manual baseline for
  official source references from every checked-in source profile.
- `reports/data-go-kr/runtime-evidence-growth.json` measures current runtime
  evidence against the 10% target and validates the next planned verification
  batches.
- `reports/kosis/runtime-evidence-plan.json`,
  `reports/ecos/runtime-evidence-plan.json`,
  `reports/open-assembly/runtime-evidence-plan.json`, and
  `reports/seoul-open-data/runtime-evidence-plan.json` record the first
  non-data.go.kr sources as `0` runtime-evidence sources with explicit blocker
  and warning IDs instead of treating missing evidence as ready.
- `reports/kosis/runtime-candidates.json`,
  `reports/ecos/runtime-candidates.json`,
  `reports/open-assembly/runtime-candidates.json`, and
  `reports/seoul-open-data/runtime-candidates.json` pin official first-batch
  runtime candidates without claiming that runtime evidence has been collected.
- `reports/source-runtime-evidence-rollup.json` rolls those source runtime
  evidence plans into a release-wide inventory of `4` sources, `0` runtime
  checks, `12` blocking blockers, and `8` warning instances after Seoul Open
  Data error taxonomy verification in Gira #67, KOSIS error taxonomy
  verification in Gira #69, ECOS error taxonomy verification in Gira #71, and
  Open Assembly error taxonomy verification in Gira #73, then runtime
  candidate batch pinning in Gira #75.
- `scripts/sync-release-schema-artifacts.py` checks that every checked-in
  `schemas/*.schema.json` file is represented in `schemas/index.json` and
  `manifest.json`; Gira #77 raises release schema coverage from `20` to `28`
  artifacts while keeping readiness warnings at `0`.
- `reports/registry-impact-plan.json` now carries a
  `registry:schema-release-surface` impact entry, and
  `scripts/validate-impact-plans.py` fails if release readiness reports schema
  coverage beyond datapan-cli's known schema set without a downstream impact
  action. Gira #79 tracks this as a datapan-cli manual investigation while
  keeping Dataset API, SDK, MCP, and datapan-data actions at `no_action`.
- `reports/coverage.json` reports high callable-operation coverage.
- `reports/route-disposition.json` separates dead-route candidates from
  transient failures.
- `reports/error-catalog.json` inventories response error/status fields.
- GitHub Actions verifies the current release surface with Git LFS enabled.

Current gaps:

- Source identity is still mostly implicit in paths and provider names.
- `sources/data_go_kr.json` establishes the baseline source profile, and the
  first non-data.go.kr profile batch is checked in.
- data.go.kr gateway coverage and data.go.kr external endpoint coverage are
  documented, but not yet generated by `datapan-cli` as source-scoped release
  artifacts.
- Error inventory has draft action routing for data.go.kr and the first
  non-data.go.kr profile batch. Gira #63 adds source-scoped runtime evidence
  plans for those non-data.go.kr sources, and Gira #65 rolls them up so release
  operators can see the remaining blocker and warning IDs centrally. Actual
  runtime evidence remains `0` for each source until adapters, credentials,
  sample parameters, and source-scoped candidate artifacts are in place.
  Gira #67 verifies Seoul Open Data's official RESULT-code taxonomy and reduces
  `source_runtime_error_taxonomy_pending` from `4` sources to `3`.
  Gira #69 verifies KOSIS official `err`/`errMsg` taxonomy and reduces
  `source_runtime_error_taxonomy_pending` from `3` sources to `2`.
  Gira #71 verifies ECOS official `RESULT.CODE`/`RESULT.MESSAGE` taxonomy and
  reduces `source_runtime_error_taxonomy_pending` from `2` sources to `1`.
  Gira #73 verifies Open Assembly official `RESULT.CODE`/`RESULT.MESSAGE`
  taxonomy and reduces `source_runtime_error_taxonomy_pending` from `1` source
  to `0`.
  Gira #75 pins official runtime candidate batches for KOSIS, ECOS, Open
  Assembly, and Seoul Open Data, reducing
  `source_runtime_manual_samples_unpinned` from `4` sources to `0` and removing
  `sample_parameters_not_pinned`/`runtime_catalog_not_materialized` blockers
  for the candidate-batch stage. Actual runtime evidence remains `0` for each
  source until adapters, credentials, and bounded runtime runs exist.
- Runtime evidence coverage is much lower than callable coverage. Gira #19,
  Gira #21, Gira #23, Gira #25, Gira #27, Gira #29, Gira #31, Gira #33, and
  Gira #35 raise data.go.kr runtime evidence from `256` to `626`. Gira #39,
  Gira #41, Gira #43, Gira #45, Gira #49, Gira #51, Gira #53, Gira #55, Gira
  #57, Gira #59, and Gira #61 continue gateway boundary evidence growth to
  `1221`, meeting the `10%` runtime evidence target and clearing the release
  readiness warning. Most of this growth is skipped boundary evidence, not
  proof that those operations are callable.
- Multi-source report grouping is designed but not implemented.
- Impact plans are specified, a data.go.kr draft plan is checked in, and a
  release-wide rollup can be generated from source-scoped plans, but full
  `datapan-cli` generation from catalog diffs, verification evidence, route
  disposition, and promoted dataset mappings is not implemented.
- Live drift checks for official source documentation are not implemented, but
  checked-in source reference baselines are now validated against source
  profiles.
- The registry release surface now includes every checked-in registry schema,
  but the current datapan-cli release readiness gate still reports `expected=20`
  and `actual=28` for `schema_set_complete`, which means CLI-side schema
  generator knowledge must be updated before draft-local releases can be the
  sole source of truth for these newer registry contracts. Gira #79 makes that
  mismatch an explicit impact-plan action instead of a remembered PR note.

## Gap Matrix

| Gap | Current artifact | Target artifact | Measurement |
| --- | --- | --- | --- |
| Source identity | `provider` strings and paths | `sources/<source_id>.json` | number of supported sources with valid source profiles |
| data.go.kr mastery | coverage reports plus route evidence | `sources/data_go_kr.json` and data.go.kr mastery gates | gateway, external registered, external dead/transient, and evidence-adjusted adapter coverage |
| Official references | documentation only | profile reference URLs with review dates | profiles with homepage/API/key/notice/terms references |
| Site behavior | adapter code and manual knowledge | source profile auth/request/response/runtime sections | profiles covering auth, paging, response, errors, runtime |
| Error routing | `reports/error-catalog.json` | `reports/<source>/error-action-catalog.json` | known error signatures mapped to action rules |
| Multi-source layout | root `data/` and `reports/` | source-scoped reports plus root rollups | source-scoped artifact count and release rollup coverage |
| Runtime confidence | `latest-verification.json` | scheduled source/provider verification matrix | evidence coverage percentage and provider pass/fail trend |
| Downstream impact | draft data.go.kr impact plan plus human review | generated `reports/registry-impact-plan.json` rollup | changes with explicit downstream action hints |
| Drift monitoring | manual source reference baseline | source reference drift reports plus scheduled checks | official reference URLs checked and classified |

## Milestones

### M1: Contract Baseline

Goal: make the target operating model explicit without migrating generated
artifacts.

Done when:

- multi-source layout is documented;
- source standardization research exists;
- `datapan.source-profile.v1` exists;
- `datapan.error-action-catalog.v1` exists;
- `datapan.registry-impact-plan.v1` exists;
- guarded release-draft workflow exists;
- current release verification stays green.

### M2: data.go.kr Mastery

Goal: master data.go.kr first as the reference implementation for source
profiles, source-scoped reports, coverage gates, error routing, and downstream
impact boundaries.

Done when:

- `sources/data_go_kr.json` exists and validates;
- source profile validation runs in CI;
- `docs/data-go-kr-mastery-plan.md` defines gateway and external endpoint
  coverage separately;
- data.go.kr missing external routes are governed by route-disposition evidence
  before becoming adapter work;
- data.go.kr source-scoped artifacts have an explicit generation contract;
- checked-in data.go.kr source-scoped reports validate in CI.

### M3: External Endpoint Coverage

Goal: turn data.go.kr external endpoint evidence and observed failures into
operational decisions before creating adapter backlog.

Done when:

- data.go.kr credential and approval failures have draft action rules;
- data.go.kr external route disposition reasons are mapped to action
  classifications;
- raw external adapter coverage and evidence-adjusted adapter candidates are
  reported separately;
- missing external routes without route-disposition evidence fail validation or
  become tracked warnings;
- error action catalog validation runs in CI;
- known credential, approval, rate limit, not-found, upstream, parser, and
  adapter cases have explicit actions;
- route-disposition and verification evidence can reference action rules;
- unknown signatures are counted instead of silently ignored.

### M4: Multi-Source Standardization

Goal: prove the source profile contract against official public data sites
outside data.go.kr without forcing data.go.kr-only assumptions onto them.

Done when:

- `sources/kosis.json` exists;
- `sources/ecos.json` exists;
- `sources/open_assembly.json` exists;
- `sources/seoul_open_data.json` exists;
- every profile validates against `datapan.source-profile.v1`;
- every profile has official reference URLs and `last_reviewed_at`;
- every profile records auth, request, response, errors, and runtime policy;
- every checked-in source-specific error action catalog validates and
  cross-checks its source profile identity;
- every checked-in source runtime evidence plan validates and records explicit
  blocker and warning IDs while evidence is absent;
- the release-wide source runtime evidence rollup validates against checked-in
  source plans;
- source-scoped reports are generated under `reports/<source>/`;
- root reports are documented as release-wide rollups;
- CI validates source-scoped report paths where present;
- the existing `data/data-go-kr.registry.json` compatibility path remains
  valid.

### M5: Client Server Impact Plans

Goal: make registry changes actionable for datapan-cli, datapan-api, SDK, and
MCP consumers.

Done when:

- data.go.kr changes can produce impact-plan entries from catalog diff,
  verification evidence, route disposition, and promoted dataset mappings;
- checked-in impact plans validate in CI before client/server consumers act on
  them;
- `reports/registry-impact-plan.json` is generated from checked-in
  source-scoped impact plans, and future CLI generation can replace that rollup
  with output derived from registry diffs, verification evidence, source
  profiles, error action catalogs, and promoted dataset mappings;
- registry-only additions can explicitly produce `no_action`;
- promoted dataset schema changes can explicitly produce
  `db_migration_review`;
- served dataset changes can explicitly target Dataset API, SDK, and MCP
  regeneration.

### M6: Drift and Evidence Growth

Goal: move from release-time confidence to ongoing registry health.

Done when:

- official reference drift reports exist;
- source profile reference changes fail CI unless the drift baseline is
  refreshed;
- scheduled health checks include source reference drift and provider runtime
  verification;
- live source reference drift checks run outside ordinary PR validation so
  external site outages are visible health failures without making every PR
  nondeterministic;
- data.go.kr runtime evidence growth is measured by a checked-in source-scoped
  report before additional verification batches are executed;
- data.go.kr runtime evidence coverage trends toward the documented `10%`
  target;
- external adapter coverage trends toward the documented `98%` target;
- warning annotations in CI are treated as work items, not background noise.

### Later: Broad Source Expansion

Goal: prove the source profile contract against diverse official sites.

Done when:
- data.go.kr mastery gates are stable;
- at least three materially different non-data.go.kr source profiles validate;
- source-scoped reports and impact plans can represent those sources without
  changing the data.go.kr compatibility surface.

## Task Backlog

Use this order unless a production failure changes priority:

1. Add and validate `sources/data_go_kr.json`. Done in PR #4.
2. Add profile validation to CI. Done in PR #4.
3. Add data.go.kr error action catalog draft. Done in PR #4.
4. Add evidence-adjusted external coverage summary for data.go.kr. Done in
   PR #4 as a checked-in draft artifact.
5. Generate data.go.kr source-scoped release artifacts. Done in PR #4 and
   tracked by Gira #5.
6. Operationalize data.go.kr external endpoint evidence. Done in PR #4 and
   tracked by Gira #6.
7. Add hand-reviewed profiles for KOSIS, ECOS, Open Assembly, and Seoul Open
   Data. Done in PR #4 and tracked by Gira #7.
8. Add and validate a data.go.kr impact plan for CLI and API consumers. Done
   in PR #4 and tracked by Gira #8.
9. Add draft `reports/<source>/error-action-catalog.json` files for M4
   sources. Tracked by Gira #9.
10. Add action catalog validation to CI. Done in PR #4 for checked-in draft
   catalogs.
11. Add source reference drift report schema and manual baseline. Tracked by
    Gira #11.
12. Add a manual or scheduled drift-check workflow. Tracked by Gira #13.
13. Add a data.go.kr runtime evidence growth summary. Tracked by Gira #15.
14. Expand runtime verification evidence by source/provider priority. Started
    by Gira #19 with `epost` and `ulsan` external endpoint batches and
    continued by Gira #21 with gateway, `geoje`, `jeonju`, and `q-net`
    batches, then by Gira #23 with `ekape`, `emuseum`, `uiryeong`, `epost`,
    and `ulsan` batches, by Gira #25 with the next gateway, `ekape`, `geoje`,
    `jeonju`, `q-net`, and `uiryeong` batches, by Gira #27 with the next
    gateway, `ekape`, `geoje`, `jeonju`, `q-net`, and `uiryeong` batches, and
    by Gira #29 with the next external `ekape`, `geoje`, `jeonju`, `q-net`, and
    `uiryeong` batches, by Gira #31 with the next `jeonju` and `q-net`
    batches, by Gira #33 with another `jeonju` and `q-net` batch, and by Gira
    #35 with the remaining planned `jeonju` and `q-net` external candidates,
    then by Gira #39, Gira #41, Gira #43, Gira #45, Gira #49, Gira #51, Gira
    #53, Gira #55, Gira #57, Gira #59, and Gira #61 with gateway batches;
    this is skipped boundary evidence growth, not proof that those operations
    are callable.
15. Add a release-wide registry impact plan rollup generated from checked-in
    source-scoped impact plans. Tracked by Gira #47; this establishes the
    client/server artifact path but does not complete full datapan-cli
    catalog-diff-based generation.
16. Add source-scoped runtime evidence plans for the first non-data.go.kr
    sources. Tracked by Gira #63; this records why KOSIS, ECOS, Open Assembly,
    and Seoul Open Data have `0` runtime checks and what must be built before
    evidence can be collected.
17. Add a release-wide source runtime evidence rollup. Tracked by Gira #65;
    this centralizes non-data.go.kr runtime evidence blockers and warnings
    without treating missing evidence as ready.
18. Verify Seoul Open Data error taxonomy from official RESULT-code references.
    Tracked by Gira #67; this reduces one `source_runtime_error_taxonomy_pending`
    warning while leaving remaining non-data runtime evidence blockers explicit.
19. Verify KOSIS error taxonomy from official `err`/`errMsg` references.
    Tracked by Gira #69; this reduces one more
    `source_runtime_error_taxonomy_pending` warning while preserving runtime
    evidence, adapter, and sample-parameter warnings.
20. Verify ECOS error taxonomy from official `RESULT.CODE`/`RESULT.MESSAGE`
    references. Tracked by Gira #71; this reduces one more
    `source_runtime_error_taxonomy_pending` warning while preserving runtime
    evidence, adapter, and sample-parameter warnings.
21. Verify Open Assembly error taxonomy from official
    `RESULT.CODE`/`RESULT.MESSAGE` references. Tracked by Gira #73; this clears
    the remaining `source_runtime_error_taxonomy_pending` warning while
    preserving runtime evidence, adapter, and sample-parameter warnings.
22. Add non-data source runtime candidate batches for KOSIS, ECOS, Open
    Assembly, and Seoul Open Data. Tracked by Gira #75; this validates pinned
    registry-only first-batch candidates, clears manual sample warnings, and
    leaves runtime evidence collection gated by adapters and credentials.
23. Bind all checked-in registry schemas into release schema artifacts. Tracked
    by Gira #77; this raises schema artifact coverage from `20` to `28`, adds a
    CI drift check, and keeps runtime warning IDs unchanged.
24. Add a registry schema release impact gate. Tracked by Gira #79; this
    requires `reports/registry-impact-plan.json` to carry
    `registry:schema-release-surface` whenever readiness reports
    `schema_set_complete.actual > expected`, and preserves `no_action`
    boundaries for Dataset API, SDK, MCP, and datapan-data.

## Measurement Rules

Each task should report at least one measurable outcome:

- profiles added or validated;
- official reference URLs covered;
- error signatures classified;
- unknown error signatures remaining;
- source-scoped reports generated;
- source runtime evidence blocker and warning IDs tracked;
- runtime verification checks added;
- evidence coverage percentage;
- adapter coverage percentage;
- downstream impact changes classified;
- CI warnings removed.

Avoid "support more sources" as a task description. Prefer "add validated
source profiles for KOSIS and ECOS" or "classify ECOS credential and not-found
errors".

Every non-trivial PR should include a short gap statement in its description:

- milestone targeted;
- gap reduced;
- artifact or gate changed;
- metric changed or expected to change;
- warnings introduced, resolved, or explicitly tracked.

## Decision Rules

- Do not add a source importer before a source profile exists.
- Do not add generated source reports before the source-specific report layout
  is clear.
- Do not convert an error into adapter work until the action catalog separates
  credential, approval, route, upstream, parser, and adapter causes.
- Do not require downstream migrations for registry-only additions.
- Do not hide failed or skipped verification evidence when it explains a real
  provider boundary.
- Do not update `manifest.json` or `schemas/index.json` by hand for generated
  release artifacts.
- Do not treat a warning as harmless merely because the workflow succeeds.

## Review Cadence

Review this blueprint when:

- a new source family is added;
- a release gate changes;
- runtime evidence targets change;
- a downstream repository starts consuming a new registry artifact;
- CI emits a new warning annotation;
- official source documentation changes in a way that affects contracts.
