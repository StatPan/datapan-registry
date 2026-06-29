# Registry Standardization Blueprint

`datapan-registry` should become the public data standardization ledger for
Datapan. The repository should make source contracts, coverage, evidence,
errors, and downstream impact explicit enough that data work can be reviewed,
released, and monitored without relying on tribal knowledge.

This blueprint is the planning layer above the individual schemas and reports.
Every registry PR should be able to explain which gap in this document it
reduces.

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
- `reports/coverage.json` reports high callable-operation coverage.
- `reports/route-disposition.json` separates dead-route candidates from
  transient failures.
- `reports/error-catalog.json` inventories response error/status fields.
- GitHub Actions verifies the current release surface with Git LFS enabled.

Current gaps:

- Source identity is still mostly implicit in paths and provider names.
- Source-specific operating profiles are not checked in yet.
- Error inventory does not yet route to operational actions.
- Runtime evidence coverage is much lower than callable coverage.
- Multi-source report grouping is designed but not implemented.
- Impact plans are specified but not generated.
- Drift checks for official source documentation are not implemented.

## Gap Matrix

| Gap | Current artifact | Target artifact | Measurement |
| --- | --- | --- | --- |
| Source identity | `provider` strings and paths | `sources/<source_id>.json` | number of supported sources with valid source profiles |
| Official references | documentation only | profile reference URLs with review dates | profiles with homepage/API/key/notice/terms references |
| Site behavior | adapter code and manual knowledge | source profile auth/request/response/runtime sections | profiles covering auth, paging, response, errors, runtime |
| Error routing | `reports/error-catalog.json` | `reports/<source>/error-action-catalog.json` | known error signatures mapped to action rules |
| Multi-source layout | root `data/` and `reports/` | source-scoped reports plus root rollups | source-scoped artifact count and release rollup coverage |
| Runtime confidence | `latest-verification.json` | scheduled source/provider verification matrix | evidence coverage percentage and provider pass/fail trend |
| Downstream impact | catalog diff and human review | `reports/registry-impact-plan.json` | changes with explicit downstream action hints |
| Drift monitoring | weekly release health | source reference drift reports | official reference URLs checked and classified |

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

### M2: Hand-Reviewed Source Profiles

Goal: prove the source profile contract against diverse official sites.

Done when:

- `sources/data_go_kr.json` exists;
- `sources/kosis.json` exists;
- `sources/ecos.json` exists;
- `sources/open_assembly.json` exists;
- `sources/seoul_open_data.json` exists;
- every profile validates against `datapan.source-profile.v1`;
- every profile has official reference URLs and `last_reviewed_at`;
- every profile records auth, request, response, errors, and runtime policy.

### M3: Error Action Routing

Goal: turn observed errors into operational decisions.

Done when:

- source-specific draft error action catalogs exist for the M2 sources;
- known credential, approval, rate limit, not-found, upstream, parser, and
  adapter cases have explicit actions;
- route-disposition and verification evidence can reference action rules;
- unknown signatures are counted instead of silently ignored.

### M4: Source-Scoped Evidence

Goal: stop treating all evidence as a single `data.go.kr` root report.

Done when:

- source-scoped reports are generated under `reports/<source>/`;
- root reports are documented as release-wide rollups;
- CI validates source-scoped report paths where present;
- the existing `data/data-go-kr.registry.json` compatibility path remains
  valid.

### M5: Impact Plan Generation

Goal: make registry changes actionable for CLI/Data/API/SDK/MCP consumers.

Done when:

- `reports/registry-impact-plan.json` is generated from registry diffs,
  verification evidence, source profiles, error action catalogs, and promoted
  dataset mappings;
- registry-only additions can explicitly produce `no_action`;
- promoted dataset schema changes can explicitly produce
  `db_migration_review`;
- served dataset changes can explicitly target Dataset API, SDK, and MCP
  regeneration.

### M6: Drift and Evidence Growth

Goal: move from release-time confidence to ongoing registry health.

Done when:

- official reference drift reports exist;
- scheduled health checks include source reference drift and provider runtime
  verification;
- runtime evidence coverage trends toward the documented `10%` target;
- external adapter coverage trends toward the documented `98%` target;
- warning annotations in CI are treated as work items, not background noise.

## Task Backlog

Use this order unless a production failure changes priority:

1. Add `sources/` and hand-reviewed profiles for M2 sources.
2. Add profile validation to CI.
3. Add source profile references to release draft documentation.
4. Add draft `reports/<source>/error-action-catalog.json` files for M2
   sources.
5. Add action catalog validation to CI.
6. Add source reference drift report schema.
7. Add a manual or scheduled drift-check workflow.
8. Update `datapan-cli` to generate source-scoped reports.
9. Update `datapan-cli` to generate registry impact plans.
10. Expand runtime verification evidence by source/provider priority.

## Measurement Rules

Each task should report at least one measurable outcome:

- profiles added or validated;
- official reference URLs covered;
- error signatures classified;
- unknown error signatures remaining;
- source-scoped reports generated;
- runtime verification checks added;
- evidence coverage percentage;
- adapter coverage percentage;
- downstream impact changes classified;
- CI warnings removed.

Avoid "support more sources" as a task description. Prefer "add validated
source profiles for KOSIS and ECOS" or "classify ECOS credential and not-found
errors".

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

## Review Cadence

Review this blueprint when:

- a new source family is added;
- a release gate changes;
- runtime evidence targets change;
- a downstream repository starts consuming a new registry artifact;
- CI emits a new warning annotation;
- official source documentation changes in a way that affects contracts.
