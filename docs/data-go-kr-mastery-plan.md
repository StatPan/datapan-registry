# data.go.kr Mastery Plan

`data.go.kr` is the baseline source for Datapan registry standardization. Before
adding broad multi-source coverage, this source must be mastered as the
reference implementation for coverage, evidence, external endpoint handling,
and downstream impact planning.

This plan uses the current release artifacts as the operating baseline.

## Baseline

Current release metrics:

- specs: `12060`
- operations: `12433`
- callable operations: `12291` (`98.9%`)
- data.go.kr gateway operations: `11419`
- external endpoint operations: `823`
- registered adapter operations: `813`
- missing adapter operations: `29`
- external adapter coverage: `96.6%`
- approval-required operations: `4322`
- no-endpoint operations: `123`
- service-root operations: `19`
- unsupported-protocol operations: `42`
- registered adapter hosts: `43`
- missing adapter hosts: `11`
- call-capable adapters: `23`

Current missing external route evidence:

- routes: `29`
- hosts: `11`
- with probe evidence: `29`
- dead-route candidates: `14`
- transient failures: `15`
- remaining adapter candidates: `0`

The practical interpretation is important: the remaining `29` missing external
routes are all covered by manifest-bound probe and route-disposition evidence.
There are currently `0` routes with adapter-candidate evidence after
`www.safetydata.go.kr` was registered as a Safety Data adapter. Dead-route and
transient-failure routes remain evidence, not implementation work, until fresh
probe evidence changes their disposition.

## Mastery Target

data.go.kr is mastered when the registry can prove all of the following:

1. Gateway coverage is explicit.
   - Operations routed through `apis.data.go.kr` are classified separately from
     external endpoints.
   - Service-key auth, paging, response envelope, status fields, and approval
     requirements are represented in `sources/data_go_kr.json`.

2. External coverage is explicit.
   - Every external endpoint host is classified as registered adapter,
     missing adapter, dead-route candidate, transient failure,
     service-root-only, approval-required, malformed, or unsupported protocol.
   - The registry does not count dead or transient routes as unknown adapter
     work.

3. Error handling is actionable.
   - Result/status fields from `reports/error-catalog.json` map to an error
     action catalog.
   - Credential and approval errors are not routed to parser or adapter work.
   - Unknown error signatures are counted and reduced.

4. Runtime evidence grows.
   - Verification evidence expands by priority, not by random sampling.
   - Gateway operations, registered external adapters, and promoted datasets
     have distinct verification targets.

5. Downstream impact is bounded.
   - Registry-only additions produce `no_action` for datapan-api, SDK, and MCP.
   - Promoted or served dataset changes produce explicit review/regeneration
     actions through the registry impact plan.

## Coverage Model

Use these coverage buckets for data.go.kr:

| Bucket | Meaning | Primary artifact |
| --- | --- | --- |
| `gateway` | Operations callable through data.go.kr gateway hosts. | `reports/coverage.json`, `reports/dependencies.json` |
| `external_registered` | External endpoint operations covered by registered adapters. | `data/provider-index.json`, `reports/coverage.json` |
| `external_missing` | External endpoint operations that appear to need adapters before route evidence. | `reports/adapter-targets.json` |
| `external_dead` | Missing external routes with dead-route probe evidence. | `reports/route-disposition.json` |
| `external_transient` | Missing external routes with timeout, DNS, request, or temporary HTTP failure evidence. | `reports/route-disposition.json` |
| `approval_required` | Operations blocked by approval or key policy. | `reports/coverage.json`, future error action catalog |
| `unsupported_protocol` | SOAP, WMS, malformed, or unsupported operation classes. | `reports/dependencies.json`, `reports/catalog-audit.json` |
| `no_endpoint` | Catalogue entries without callable endpoint metadata. | `reports/catalog-audit.json` |

The registry should report external coverage using both raw and evidence-adjusted
views:

- raw external adapter coverage: registered adapter operations divided by
  external endpoint operations;
- evidence-adjusted adapter candidates: missing external routes excluding
  dead-route candidates and transient failures with current probe evidence.
- operational gate: missing external routes without route-disposition evidence
  must fail validation before they can become adapter backlog.

## External Endpoint Spec

External endpoint coverage is a first-class data.go.kr concern. It is not a
separate source just because the endpoint host is outside `apis.data.go.kr`.

Each external host should have:

- host identity;
- source dataset ids and operation ids;
- dependency class;
- registered adapter status;
- call and verification capability;
- route-disposition status when missing;
- probe evidence timestamp and reason when unavailable;
- error action rule when failures are known;
- downstream impact when promoted datasets depend on the host.

Registered external adapters currently cover these host families through
`data/provider-index.json`: airport, andong, culture, data-gg, ekape, emuseum,
epost, folk, forest, garak, gblib, geoje, gwanak, happysd, humetro, i815,
itfind, jeju, jeonju, korad, kpx, lh-ebid, mafra, myhome, naqs, ncpms, nfqs,
nongsaro, oneclick-law, pqis, q-net, safetydata, seoul-bus, seoul-open-data,
sisul, tour, uiryeong, ulsan, and work24.

Missing external route hosts currently requiring route-disposition tracking:

- `openapi.coast.kr`: `6`
- `car.daegu.go.kr`: `4`
- `openapi.price.go.kr`: `4`
- `www.rda.go.kr`: `4`
- `its.gyeongju.go.kr:81`: `3`
- `data.wanju.go.kr`: `2`
- `www.cid.or.kr`: `2`
- `openapi-lib.sen.go.kr`: `1`
- `www.dgeic.or.kr:8080`: `1`
- `www.jobplustv.or.kr`: `1`
- `www.simpan.go.kr`: `1`

These hosts should become adapter implementation tasks only for routes with
`adapter_candidate` evidence. Dead and transient routes stay in the
route-disposition ledger until fresh probe evidence changes their status.

## Required Artifacts

data.go.kr mastery should produce or preserve:

- `sources/data_go_kr.json`
- `data/data-go-kr.registry.json`
- `data/provider-index.json`
- `reports/coverage.json`
- `reports/dependencies.json`
- `reports/catalog-audit.json`
- `reports/error-catalog.json`
- `reports/adapter-targets.json`
- `reports/route-disposition.json`
- `reports/data-go-kr/external-coverage-summary.json`
- `reports/data-go-kr/external-adapter-backlog.json`
- `reports/latest-verification.json`
- `reports/latest-verification-summary.json`
- `reports/data-go-kr/error-action-catalog.json`
- `reports/data-go-kr/registry-impact-plan.json`
- `reports/registry-impact-plan.json`
- `reports/data-go-kr/runtime-evidence-growth.json`
- `reports/data-go-kr/operation-materialization-plan.json`
- `reports/data-go-kr/safetydata-operation-candidates.json`
- `reports/data-go-kr/safetydata-registry-patches.json`
- `reports/data-go-kr/institution-api-overview.json`
- `reports/data-go-kr/institution-runtime-plan.json`
- `docs/data-go-kr-external-adapter-backlog.md`

## Source-Scoped Generation Contract

Checked-in data.go.kr source-scoped artifacts must name the root reports they
are generated from, then CI must verify that the source-scoped values still
match those roots.

| Source-scoped artifact | Required generation inputs | CI gate |
| --- | --- | --- |
| `reports/data-go-kr/external-coverage-summary.json` | `sources/data_go_kr.json`, `reports/coverage.json`, `reports/adapter-targets.json`, `reports/route-disposition.json`, `data/provider-index.json` | `scripts/generate-external-coverage-summary.py` regenerates the report, and `scripts/validate-external-coverage.py` validates schema and cross-checks source identity, raw coverage metrics, route evidence counts, adapter target counts, provider-index host count, and missing host counts. |
| `reports/data-go-kr/external-adapter-backlog.json` | `sources/data_go_kr.json`, `reports/route-disposition.json`, `reports/adapter-targets.json`, `reports/coverage.json` | `scripts/generate-external-adapter-backlog.py` regenerates the host/API implementation backlog, and `scripts/validate-external-adapter-backlog.py` fails if adapter candidates, excluded dead/transient routes, host counts, or markdown drift from route-disposition evidence. |
| `reports/data-go-kr/error-action-catalog.json` | `sources/data_go_kr.json`, `reports/error-catalog.json`, `reports/route-disposition.json`, provider verification reports | `scripts/validate-error-action-catalogs.py` validates checked-in action rules; future generation should also fail on unmapped known error signatures. |
| `reports/data-go-kr/registry-impact-plan.json` | `sources/data_go_kr.json`, catalog diff, verification evidence, route disposition, error action catalog, promoted dataset mappings | `scripts/validate-impact-plans.py` validates schema, summary counts, target counts, identity fields, and promoted/served dataset boundaries before client/server consumers act on it. |
| `reports/registry-impact-plan.json` | checked-in `reports/*/registry-impact-plan.json` source plans | `scripts/generate-impact-plan-rollup.py` generates the release-wide rollup, and `scripts/validate-impact-plans.py` validates mixed-source release scope while preserving strict source scope for source-specific plans. |
| `reports/data-go-kr/runtime-evidence-growth.json` | `reports/coverage.json`, `reports/latest-verification.json`, `reports/latest-verification-summary.json`, `reports/verification-plan.json`, `data/provider-index.json` | `scripts/validate-runtime-evidence-growth.py` validates current evidence totals, evidence coverage percent, 10% target gap, provider split readiness, and next planned verification batches. |
| `reports/data-go-kr/operation-materialization-plan.json` | `reports/data-go-kr/coverage-backlog.json` | `scripts/validate-operation-materialization-plan.py` regenerates the institution-scoped materialization queue and fails CI when APIs without operation mappings drift from the coverage backlog. |
| `reports/data-go-kr/safetydata-operation-candidates.json` | `reports/data-go-kr/operation-materialization-batches/institution-01.json`, public data.go.kr and safetydata.go.kr metadata | `scripts/validate-safetydata-operation-candidates.py` validates the checked-in candidate evidence schema and batch linkage; refresh is manual or workflow-dispatched because it depends on live public metadata. |
| `reports/data-go-kr/safetydata-registry-patches.json` | `reports/data-go-kr/safetydata-operation-candidates.json`, `data/data-go-kr.registry.json` | `scripts/validate-safetydata-registry-patches.py` validates exact operation payloads before registry mutation and keeps already-applied Safety Data mappings reproducible after mutation. |
| `reports/data-go-kr/institution-api-overview.json` | `data/data-go-kr.registry.json`, `reports/dependencies.json`, `reports/latest-verification.json`, `reports/coverage.json`, `data/provider-index.json` | `scripts/validate-institution-api-overview.py` regenerates the overview and fails CI when institution API counts, operation counts, adapter status counts, or runtime evidence counts drift from checked-in artifacts. |
| `reports/data-go-kr/institution-runtime-plan.json` | `reports/data-go-kr/coverage-backlog.json`, `data/data-go-kr.registry.json`, `reports/latest-verification.json` | `scripts/validate-institution-runtime-plan.py` regenerates the executable institution batch plan and fails CI when the next `datapan catalog verify --org` queue drifts from the coverage backlog. |

This contract keeps `data/data-go-kr.registry.json` as the compatibility
registry path while moving generated evidence toward `reports/data-go-kr/`.
If any root report changes without the source-scoped artifact being refreshed,
CI should fail rather than treating the checked-in summary as authoritative.

## Institution-Scoped Coverage Loop

The registry now tracks API and runtime coverage at the institution level with
two generated views:

- `docs/data-go-kr-institution-api-overview.md` shows each institution's API
  count, operation count, runtime evidence count, evidence percentage, and
  top hosts/categories.
- `docs/data-go-kr-coverage-backlog.md` ranks institutions by uncovered API
  and runtime reactivation gaps.
- `docs/data-go-kr-operation-materialization-plan.md` turns APIs without
  operation mappings into bounded institution work queues.
- `docs/data-go-kr-institution-runtime-plan.md` turns the top runtime gaps into
  bounded `datapan catalog verify --org` commands.

Use those views as the runtime reactivation queue. For each selected
institution:

1. Run a bounded verification batch with `datapan catalog verify --org <기관명>`.
2. Merge the batch into `reports/latest-verification.json`.
3. Regenerate `reports/latest-verification-summary.json`.
4. Regenerate `reports/data-go-kr/coverage-backlog.json` and
   `reports/data-go-kr/institution-api-overview.json`.
5. Regenerate `reports/data-go-kr/institution-runtime-plan.json`.
6. Run `scripts/validate-coverage-backlog.py`,
   `scripts/validate-institution-runtime-plan.py`, and
   `scripts/validate-institution-api-overview.py`.

The current first queue is `행정안전부`: `1252` APIs, `793` APIs with operation
mappings, `459` uncovered APIs, `1054` mapped operations, and no checked-in
runtime evidence yet. Gateway calls need a data.go.kr service key; no-key runs
are useful only to prove parameter readiness, not to advance verified runtime
coverage.

## Task Sequence

1. Add and validate `sources/data_go_kr.json`. Done in PR #4.
2. Add source profile validation to CI. Done in PR #4.
3. Add a data.go.kr error action catalog draft. Done in PR #4.
4. Connect route-disposition reasons to error/action classifications. Started
   in `reports/data-go-kr/error-action-catalog.json`.
5. Add an evidence-adjusted external coverage summary. Done in PR #4 and made
   reproducible with `scripts/generate-external-coverage-summary.py` in Gira
   #91.
6. Add source-scoped generation input cross-checks for data.go.kr external
   coverage. Done in PR #4 and extended with generator-backed maintenance in
   Gira #91.
7. Add an operational gate that fails validation for missing external routes
   without route-disposition evidence and permits adapter backlog only from
   adapter-candidate evidence. Done in PR #4.
8. Generate the source-scoped external adapter implementation backlog directly
   from route-disposition adapter candidates. Done in Gira #95.
9. Add a runtime evidence growth summary that measures current evidence
   coverage against the 10% target and validates the next planned batches.
   Tracked by Gira #15.
10. Maintain institution-scoped coverage and runtime reactivation queues from
    the generated overview/backlog artifacts. The first active queue is
    `행정안전부`, using `datapan catalog verify --org 행정안전부` batches after
    data.go.kr credentials are available.
11. Execute the next runtime verification batches for gateway and registered
   external adapters. Started by Gira #19 with `epost` and `ulsan`
   excluded-from-latest external endpoint batches. This grows checked runtime
   evidence from `256` to `276`, but the new records are skipped boundary
   evidence, not successful callable coverage. Continued by Gira #21 with
   gateway, `geoje`, `jeonju`, and `q-net` boundary batches, growing checked
   runtime evidence to `316`. Continued by Gira #23 with `ekape`, `emuseum`,
   `uiryeong`, `epost`, and `ulsan` boundary batches, growing checked runtime
   evidence to `346`. Continued by Gira #25 with gateway, `ekape`, `geoje`,
   `jeonju`, `q-net`, and `uiryeong` boundary batches, growing checked runtime
   evidence to `406`. Continued by Gira #27 with the next gateway, `ekape`,
   `geoje`, `jeonju`, `q-net`, and `uiryeong` boundary batches, growing checked
   runtime evidence to `466`. Continued by Gira #29 with the next external
   `ekape`, `geoje`, `jeonju`, `q-net`, and `uiryeong` boundary batches,
   growing checked runtime evidence to `499`. Continued by Gira #31 with the
   next `jeonju` and `q-net` boundary batches, growing checked runtime evidence
   to `519`. Continued by Gira #33 with another `jeonju` and `q-net` boundary
   batch, growing checked runtime evidence to `539`. Gira #35 completed the
   remaining planned `jeonju` and `q-net` external boundary candidates, growing
   checked runtime evidence to `626`. Continued by Gira #39, Gira #41, Gira
   #43, Gira #45, Gira #49, Gira #51, Gira #53, Gira #55, Gira #57, Gira #59,
   and Gira #61 with gateway boundary batches, growing checked runtime evidence
   to `1221`. Gira #93 adds the next gateway boundary batch, growing checked
   runtime evidence to `1231` and clearing the unrounded `10%` runtime evidence
   target gap. Gira #97 adds `data-gg` external endpoint verification evidence,
   growing checked runtime evidence to `1249` and verified results to `40`.
   Gira #99 adds `nfqs` external endpoint verification evidence, growing checked
   runtime evidence to `1254` and verified results to `45`. Gira #101 adds
   `nongsaro` external endpoint verification evidence, growing checked runtime
   evidence to `1258` and verified results to `49`. Gira #103 adds `gwanak`
   external endpoint verification evidence, growing checked runtime evidence to
   `1261` and verified results to `52`. Gira #105 adds `mafra` external
   endpoint verification evidence, growing checked runtime evidence to `1264`
   and verified results to `55`. Gira #107 adds `garak` external endpoint
   verification evidence, growing checked runtime evidence to `1267` and
   verified results to `58`. Gira #109 adds `work24` external endpoint
   verification evidence, growing checked runtime evidence to `1270` and
   verified results to `61`. Gira #111 adds `seoul-open-data` adapter coverage
   and external endpoint verification evidence, growing checked runtime
   evidence to `1272`, verified results to `63`, registered external adapter
   operations to `627`, and reducing evidence-adjusted adapter candidates to
   `6`. Gira #113 adds `culture` and `happysd` external endpoint verification
   evidence, growing checked runtime evidence to `1276`, verified results to
   `67`, registered external adapter operations to `631`, and reducing
   evidence-adjusted adapter candidates to `2`. Gira #115 adds `ncpms` and
   `i815` external endpoint verification evidence, growing checked runtime
   evidence to `1278`, verified results to `69`, registered external adapter
   operations to `633`, and reducing evidence-adjusted adapter candidates to
   `0`. The Safety Data adapter then registers `www.safetydata.go.kr`,
   increasing registered external adapter operations to `638`; the next
   Safety Data materialization batches add 175 행정안전부 operation mappings,
   increasing registered external adapter operations to `813` while keeping
   evidence-adjusted adapter candidates at `0`. This is still mostly skipped
   boundary evidence, and the new
   `data-gg`/`nfqs`/`nongsaro`/`gwanak`/`mafra`/`garak`/`work24`/
   `seoul-open-data`/`culture`/`happysd`/`ncpms`/`i815` results prove landing-page
   reachability rather than generic machine-call support; the Safety Data
   operations remain approval-gated until credentials and approval state are
   available.
12. Add a data.go.kr draft impact plan and validate its client/server action
   boundaries in CI. Done in PR #4.
13. Generate future data.go.kr impact plans directly from catalog diff,
   verification evidence, route disposition, and promoted dataset mappings.
14. Add a release-wide registry impact plan rollup generated from checked-in
   source-scoped impact plans. Tracked by Gira #47.

## Done Criteria

data.go.kr mastery is complete when:

- source profile validation is enforced in CI;
- gateway and external coverage are reported separately;
- evidence-adjusted adapter candidates are reported;
- every missing external route has route disposition evidence;
- known data.go.kr credential and approval failures map to error action rules;
- runtime evidence coverage has met the `10%` target without treating skipped
  boundary evidence as callable success;
- downstream impact plans can express `no_action`, `refresh_verification`,
  `update_adapter`, and `db_migration_review` for data.go.kr changes.
