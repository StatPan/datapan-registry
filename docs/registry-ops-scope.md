# Registry Operations Scope

`datapan-registry` is the source/spec/evidence ledger for Datapan public data
work. It should become the standard place to answer these questions:

- Which public data sources exist and where are their official references?
- Which source contracts were published in a release?
- Which operations are callable, blocked, deprecated, or unstable?
- Which provider-specific errors are known and what action should they trigger?
- Which downstream systems need regeneration, review, or no action?

It should not become the runtime that fetches, stores, or serves promoted
datasets. Runtime execution belongs in `datapan-cli`, `datapan-data`, Dataset
API, SDK, MCP, and provider adapter repositories.

Use `docs/registry-standardization-blueprint.md` as the planning document for
target architecture, gap measurement, milestones, and task sequencing.
Use `docs/registry-governance-policy.md` as the quality and review policy for
each step toward that blueprint.

## Current Contract Layers

The registry should grow in layers:

1. Release artifact integrity
   - `manifest.json`
   - `schemas/index.json`
   - release verification and readiness reports

2. Source layout and official references
   - `docs/multi-source-release-layout.md`
   - `docs/source-standardization-research.md`
   - future `sources/<source_id>.json`
   - `schemas/datapan.source-profile.v1.schema.json`

3. Provider and route evidence
   - `data/provider-index.json`
   - `reports/provider-backlog.json`
   - `reports/adapter-targets.json`
   - `reports/route-disposition.json`

4. Error inventory and action routing
   - `reports/error-catalog.json`
   - future `reports/<source>/error-action-catalog.json`
   - `schemas/datapan.error-action-catalog.v1.schema.json`

5. Downstream impact planning
   - future `reports/registry-impact-plan.json`
   - `schemas/datapan.registry-impact-plan.v1.schema.json`

## Selected Near-Term Scope

The next work should focus on contracts and evidence, not broad data imports.

### 1. data.go.kr Mastery

Master `data.go.kr` before broad source expansion. The operating plan is in
`docs/data-go-kr-mastery-plan.md`.

The key split is:

- gateway operations handled through data.go.kr gateway hosts;
- external endpoint operations discovered from data.go.kr but requiring
  provider adapters or route-disposition evidence.

Missing external routes should not become adapter work until route evidence
shows they are viable adapter candidates.

### 2. Source Profiles

Define source profiles for materially different source families:

- `data_go_kr`: gateway-heavy national portal.
- `kosis`: statistical catalogue and table-oriented APIs.
- `ecos`: economic time-series API.
- `open_assembly`: legislative public data portal.
- `seoul_open_data`: municipal data portal with mixed distributions.

Each profile should include official references, auth policy, request model,
response formats, error field candidates, runtime verification policy, and
promotion defaults.

### 3. Error Action Catalogs

For each active source, turn observed provider errors into routing rules:

- credential and approval failures should not become parser work.
- 404 and dead-route candidates should not become adapter work without route
  evidence.
- response-shape and parser failures should be separated from upstream outages.
- promoted dataset impacts should be routed to manual review when storage or
served contracts may change.

### 4. Drift Evidence

Add a source-reference drift report before adding automatic source expansion.
The report should record official reference URL status, final URL, content
fingerprint, and checked timestamp. It should warn or fail based on contract
impact, not on every homepage change.

### 5. Runtime Evidence Growth

The current release has high callable coverage but shallow runtime evidence.
The near-term target should be:

- keep callable operation coverage at or above the current release level;
- raise runtime verification evidence from the current low single-digit
  percentage toward the documented `10%` target;
- prioritize source/provider families with call-capable adapters and promoted
  downstream datasets;
- keep failed and skipped evidence when it explains real provider boundaries.

## Boundaries

Registry-owned:

- source identities and official references;
- schema contracts for release artifacts;
- source profiles and provider capabilities;
- error/action classification rules;
- verification, coverage, route, and readiness evidence;
- downstream action hints.

Not registry-owned:

- API keys and credentials;
- full upstream data extraction;
- database migrations;
- collector/parser implementation;
- SDK package publishing;
- hosted Dataset API runtime behavior.

The registry may say "DB migration review required" or "provider adapter update
required". It should not perform those changes itself.

## Gate Model

The long-term gate model should include:

- release gate: manifest checksum, schema validation, readiness;
- health gate: scheduled release install and doctor checks;
- source gate: official reference drift and source profile validation;
- provider gate: adapter verification and route disposition;
- error gate: known error signatures map to action catalog rules;
- impact gate: registry changes produce downstream action hints;
- promotion gate: only promoted or served datasets can require storage/API/SDK
  review.

## PR Sizing

Keep generated data migrations separate from contract changes.

Recommended sequence:

1. Merge contract scaffolding.
2. Add source profile schema and a small set of hand-reviewed source profiles.
3. Add error action catalog schema and source-specific draft catalogs.
4. Add CLI generation for source profiles, action catalogs, and impact plans.
5. Add scheduled drift checks.
6. Expand source imports and runtime evidence coverage.

This avoids blocking the registry standard on a complete survey of every public
data site while still forcing every new source through the same evidence model.
