# Repository Instructions

## Purpose and source of truth (Datapan Core)

**Core objective** (statpan_docs `projects/datapan/STRATEGY_CONTROL.md`):
공공데이터 전반을 파악하고, 필요한 데이터를 지속적으로 확보·갱신·검증하여 여러 사업에서 재사용할 수 있게 유지·공급한다.
This repo is one part of the Datapan Core product (Registry, Data, CLI, Health, Web, Infra). The product is a maintainable public-data foundation, not a single business model, procurement product, or sample cohort.

**Read order for agents** (private repo `StatPan/statpan_docs`, read `origin/main`, not a stale local checkout; reviewed at `578aebc`, 2026-10-17):
1. `projects/datapan/README.md` — source precedence.
2. `projects/datapan/REPO_CONTRACTS.md` — this repo's goal and standing feature IDs (see "Repository development goals" and its own section).
3. `projects/datapan/PM_DECISION_POLICY.md` — how requirements become Goals/issues; acceptance and escalation.
4. `projects/datapan/core/OPERATING_MODEL.md` — production vs delivery, reuse/isolation, cost, CTR/NFR/FLEX requirements.
Precedence: accepted strategy > REPO_CONTRACTS > OPERATING_MODEL > business/ research (hypotheses, not decisions) > historical docs. Implementation truth is default-branch code/tests; operational claims need dated runtime evidence; execution state lives in GitHub Goals/issues/PRs.

**Principles that bind every change**
- Production, publication, delivery, consumer receipt and customer acceptance are separate facts. Never report one as another.
- Failed/partial/unknown/not-measured are distinct states; unmeasured cost or coverage is not zero or complete. A failed refresh must not overwrite the last good version.
- Identity, provenance, observation time, and rights stay explicit. Name similarity or model inference alone is not a fact.
- Open/DIY local use stays genuinely useful; do not weaken it to push paid use. Paid value = recurring responsibility (collect, validate, refresh, source-change response, history/recovery, delivery).
- Customer-private/specific work is isolated from shared Core; reuse needs named evidence, rights, and bounded maintenance cost.
- Do not adopt a business model, price, offer, or market from an engineering ticket. Business research is not a decision; a Core release does not prove demand.
- Opening an issue/PR does not mean it is approved or prioritized. Deployment, publication, commercial activation, secrets, and ownership changes need their own explicit approval.
- Record gaps honestly: when code, docs, or runtime disagree, open/refresh an issue rather than silently changing one side.

### Registry contract

`StatPan/datapan-registry` makes changing public sources discoverable and usable without each consumer rebuilding the source catalog: it maintains source/resource identity and discovery (REG-01), portable specifications and access-policy metadata (REG-02), observation reconciliation and change management (REG-03), evidence admission and separate coverage/readiness views (REG-04), and immutable contract release and distribution with manifests, digests, compatibility, and rollback-or-hold references (REG-05). Registry completion means tracking additions, changes, deprecations, and omissions for the declared source set, publishing verified revisions, and enabling consumers to reproduce and read the exact artifact; implementation must produce consumable artifacts, preserve the prior catalog on failed refresh, remain idempotent for repeated admitted input, and identify affected consumers for changed contracts.

Registry does not own private credentials, customer state, mutable runtime coordination, bulk provider-payload collection, domain data marts, billing, product UI, or arbitrary provider execution. It is not the collector of all payloads: current specification-workflow ownership remains until an explicit migration, while Data owns acquisition and dataset outputs and consuming repositories own their compatibility/read-back evidence.

<!-- gira:start -->
Gira workflow:
- Canonical rules: run `gira guide agent` from the installed Gira CLI.
- `gira ticket new "Title" --dry-run|--apply [--body TEXT|--body-file PATH|-] [--start]`: Create a repo-bound executable GitHub issue with structured or full Markdown body input.
- `gira ticket view|show [TICKET] [--repo OWNER/REPO] [--json]`: Show a Gira operating card for the ticket, linked PR, blockers, and next action. Alias: gira ticket show.
- `gira ticket prompt [TICKET] [planner|implementer|reviewer] [--role planner|implementer|reviewer] [--profile default|python] [--repo OWNER/REPO] [--pr N] [--json]`: Render a stateless planner, implementer, or reviewer prompt from ticket context.
- `gira ticket handoff [TICKET] [planner|implementer|reviewer] [--role planner|implementer|reviewer] [--profile default|python] [--repo OWNER/REPO] [--json]`: Compile a worker-neutral handoff packet from ticket context.
- `gira ticket review [TICKET] [--repo OWNER/REPO] [--pr N] [--diff-summary] [--include-diff] [--json|--html --output PATH]`: Render a reviewer packet from current ticket and linked PR state.
- `gira ticket start [TICKET] --dry-run|--apply [--repo OWNER/REPO] [--base BRANCH]`: Verify a ready issue, create or reuse its branch, and move it to in-progress.
- `gira ticket pr [TICKET] --dry-run|--apply [--repo OWNER/REPO] [--draft]`: Create or validate a linked PR with required issue closing text.
- `gira ticket self-review [TICKET] [--repo OWNER/REPO] [--pr N] [--diff-summary] --dry-run|--apply [--json]`: Post a self-review check note for the current branch ticket and linked PR.
- `gira ticket note [TICKET] "BODY" --dry-run|--apply [--repo OWNER/REPO] [--kind progress|blocker|decision|handoff|summary|check] [--target auto|issue|pr|both]`: Post a structured context note to the issue, linked PR, or both.
- `gira ticket supersede [TICKET] --replacement-title TITLE --body-file PATH|- --dry-run|--apply [--repo OWNER/REPO] [--close-draft-pr]`: Close a ticket as superseded and create a linked replacement ticket.
- `gira ticket checks [TICKET] [--repo OWNER/REPO] [--json]`: Show linked PR checks, review blockers, and next action.
- `gira ticket wait [TICKET] [--repo OWNER/REPO] [--timeout 5m] [--interval 5s]`: Wait for pending linked PR checks without merging.
- `gira ticket finish [TICKET] --dry-run|--apply [--repo OWNER/REPO] [--sync-local]`: Merge the linked PR when policy allows and close the ticket loop without local checkout sync by default.
- `gira ticket status [TICKET] [--repo OWNER/REPO] [--json|--html --output PATH]`: Report ticket status, linked PR blockers, and next action.
- `gira config storage [--repo OWNER/REPO] [--config-root PATH] [--json]`: Show local storage roots, durability, privacy, and rebuild boundaries.
- `gira feature check [--repo OWNER/REPO] [--limit N] [--json]`: Validate optional feature map records and work links without mutating GitHub.
- `gira feature for ISSUE [--repo OWNER/REPO] [--limit N] [--json]`: Show which feature or capability a work issue is linked to.
- `gira feature list [--repo OWNER/REPO] [--limit N] [--json]`: List optional issue-backed feature or capability records.
- `gira goal finish [GOAL] --dry-run|--apply [--repo OWNER/REPO] [--terminal done|human_review|blocked|superseded|abandoned] [--json]`: Preview goal finish readiness, then post receipts and close ready goals or preserve human-review handoffs.
- `gira goal next [GOAL] [--repo OWNER/REPO] [--json]`: Select the next safe child ticket for a goal or explain why work must stop.
- `gira goal plan [GOAL] --dry-run|--apply [--repo OWNER/REPO] [--json]`: Propose or create same-repo or target-repo child ticket packets from a goal issue.
- `gira goal report [GOAL] [--repo OWNER/REPO] [--json|--html --output PATH]`: Build a visible report for one goal from stable Goal Mode state. Alias: gira goal dossier.
- `gira goal status [GOAL] [--repo OWNER/REPO] [--json]`: Summarize a goal issue, child ticket graph, blockers, and next safe action.
- `gira queue handoff [--config .gira/config.yaml] [--repo OWNER/REPO] [--ticket N] [--role implementer] [--profile default] [--compact] [--json]`: Select or inspect an agent-ready workspace queue item and embed the worker-handoff/v1 payload.
- `gira queue list [--config .gira/config.yaml] [--repo OWNER/REPO] [--queue ready|review|finish|blocked|failed|human] [--limit N] [--compact] [--json]`: List workspace queue items derived from workspace-queues/v1.
- `gira queue next [--config .gira/config.yaml] [--repo OWNER/REPO] [--role implementer] [--profile default] [--compact] [--json]`: Select the first agent-ready workspace queue item and print handoff and run-start commands.
- `gira queue take [--config .gira/config.yaml] [--repo OWNER/REPO] [--ticket N] [--role implementer] [--profile default] [--compact] --dry-run|--apply [--json]`: Start a handoff-safe queue item through the existing ticket start policy.
- Use --dry-run before --apply for mutating Gira operations.
- Prefer Gira commands over raw gh when a Gira command exists.
- PR bodies must contain Closes #N, Fixes #N, or Resolves #N.
- Keep changes bounded to the ticket.
<!-- gira:end -->

## Agent runbook
- Run CI-compatible checks with Python 3.12, `jsonschema`, and `PyYAML==6.0.2`; direct test: `uv run --no-project --python 3.12 --with jsonschema --with pyyaml python -m unittest tests/test_apply_runtime_freshness_import.py`.
- Record the direct test's passing result (5 tests, OK) in the PR body. Completeness modules can take 15–19 minutes: select an individual test name and run long tests in the background.
- Delegate one issue and one branch per agent with explicit scope. No unauthorized live calls, Hugging Face publication, secrets, or destructive operations.
- Evidence must state: command, exit code, actual output, interpretation, and unverified items.
- R1/R2/R3 milestones and next actions are execution state, not durable contract; use the current GitHub issues and PRs as the source of truth (for example, #655/#659, #760, #768, #791, #592, and #593).
- Operational caution: publication ACK recovery must preserve the guards and read-only-first behavior tracked in [#791](https://github.com/StatPan/datapan-registry/issues/791).
