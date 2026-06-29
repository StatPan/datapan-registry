# Registry Governance Policy

`datapan-registry` is a standards repository. Its work should be governed by
policy first, naming second, and implementation third. A new artifact is useful
only when it reduces a named registry gap while preserving release quality.

This policy defines how goals are set, how gaps are measured, and how code and
data quality are kept high while the registry expands across public data
sources.

## Policy Goals

1. Coverage grows with evidence.
   - Expanding catalog coverage is not enough.
   - New sources must carry official references, source profiles, verification
     strategy, and error-action expectations.

2. Operations stay reproducible.
   - Release artifacts must be generated or verified by documented commands.
   - Generated release files must not be hand-edited to satisfy a gate.

3. Failures become routed work.
   - Credential, approval, rate-limit, upstream, parser, adapter, and schema
     failures must be distinguishable.
   - Unknown failures must be counted and reduced.

4. Downstream impact is explicit.
   - Registry-only changes should not force server/client work.
   - Promoted and served dataset changes must identify the affected downstream
     target and review type.

5. Warnings are work items.
   - CI warnings, deprecation annotations, schema drift, and reference drift are
     not background noise.
   - Each warning should be resolved, converted to a tracked task, or justified
     with an explicit owner and review date.

## Gap Ledger Rules

The blueprint gap matrix is the ledger of record. Every non-trivial PR should
state:

- the milestone it targets;
- the gap it reduces;
- the artifact or check it adds;
- the measurement that changed;
- any new gap it creates.

Acceptable gap statements:

- "M2: adds validated source profiles for KOSIS and ECOS."
- "M3: classifies ECOS credential and not-found errors."
- "M6: removes Node.js runtime deprecation warning from release verification."

Unacceptable gap statements:

- "cleanups"
- "more source support"
- "misc docs"
- "fix CI" without naming the failed gate or warning

## Quality Gates

Every PR should preserve or improve these gates:

- JSON artifacts and schemas parse with `jq`.
- GitHub workflow YAML parses.
- `git diff --check` passes.
- release verification remains green unless the PR explicitly changes the
  release gate and documents the migration path.
- new schemas use versioned names and do not mutate existing generated release
  artifacts by hand.
- source profiles cite official references.
- error action catalogs distinguish unknown signatures from known routed
  failures.
- warnings are removed or tracked.

Generated release artifacts have stricter rules:

- `manifest.json` and `schemas/index.json` are generated release artifacts.
- Do not update their checksums manually.
- If a new schema or report should become part of a release, add the schema or
  documentation first, then include it through the next generated release draft.

## Naming Policy

Naming should support policy and operations, not local taste.

- Use `source_id` for stable machine identity.
- Use lowercase snake case for `source_id`, such as `data_go_kr`, `kosis`, and
  `open_assembly`.
- Use hyphenated paths only where existing compatibility requires it, such as
  `data/data-go-kr.registry.json`.
- Keep `provider` as the upstream human-facing label, such as `data.go.kr`.
- Do not introduce a new name when `source_id`, `provider`, `host`,
  `operation_id`, or `endpoint_id` already expresses the concept.
- New report names should describe the operational question they answer:
  coverage, verification, route disposition, error action, impact plan, or
  drift.

## Review Standards

Reviewers should reject changes that:

- add a source without official references;
- add an importer before a source profile exists;
- add runtime behavior without verification evidence;
- classify provider failures without an action path;
- route credential or approval failures to parser or adapter work;
- make server/client regeneration mandatory for registry-only additions;
- suppress failed or skipped evidence to improve presentation;
- leave CI warnings unexplained.

Reviewers should ask for a gap statement when a PR's purpose is unclear.

## Client And Server Integration Policy

The registry should support both client-side and server-side consumers without
coupling itself to their runtimes.

Server-side consumers, such as `datapan-api`, should receive:

- source and endpoint identity;
- served dataset status;
- response-shape and schema-impact hints;
- action hints for parser, schema, route, and migration review.

Client-side consumers, such as SDKs and MCP tools, should receive:

- served dataset contract changes;
- regeneration hints;
- deprecation and removal signals;
- no-action signals for registry-only changes.

The registry should not publish server routes, run database migrations, or
generate SDK packages itself. It should make those downstream decisions
measurable and reviewable.

## Operating Metrics

Track these metrics as the registry matures:

- source profiles validated;
- official reference URLs tracked;
- callable operation coverage;
- external adapter coverage;
- runtime evidence coverage;
- verification pass/fail/skip counts;
- known error signatures classified;
- unknown error signatures remaining;
- source-scoped report coverage;
- registry changes with impact-plan actions;
- CI warnings open and resolved.

Targets should be reviewed in the blueprint when a milestone is completed or a
new source family changes the operating model.

## Escalation

Escalate a registry change before merge when:

- it changes release gates;
- it changes compatibility paths;
- it changes generated artifact contracts;
- it affects promoted or served dataset storage;
- it introduces a new source family without a representative profile;
- it leaves a CI warning unresolved.

Escalation can be a blocking review comment, a follow-up issue, or a separate
PR, but it must be visible.
