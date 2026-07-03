# data.go.kr Mastery Plan

`data.go.kr` is the baseline source for Datapan registry standardization. Before
adding broad multi-source coverage, this source must be mastered as the
reference implementation for coverage, evidence, external endpoint handling,
and downstream impact planning.

This plan uses the current release artifacts as the operating baseline.

## Baseline

Current release metrics:

- specs: `12060`
- operations: `18826`
- callable operations: `18684` (`99.2%`)
- data.go.kr gateway operations: `11419`
- external endpoint operations: `7216`
- registered adapter operations: `7206`
- missing adapter operations: `29`
- external adapter coverage: `99.6%`
- approval-required operations: `6661`
- no-endpoint operations: `123`
- service-root operations: `19`
- unsupported-protocol operations: `149`
- registered adapter hosts: `97`
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
`data/provider-index.json`: airport, andong, calspia, cancer, car, car365, codil, consumer, culture, data-gg, dgfca, dongjak, ekape, emuseum,
epost, eshare, ex, fairdata, folk, foodsafetykorea, forest, franchise-ftc, garak, gblib, geoje, gicoms, gimhae, gwanak,
gwangjin, gwangmyeong, happysd, humetro, i815, icheon, ins24, itfind, its, jeju, jeju-air, jeju-www, jejudatahub, jejuits, jeonnam-redtable, jeonju, juso,
kistep, kofpi, korad, kpx, lh-ebid, lofin365, mafra, mnd-open-data, myhome, nabic, naqs, ncpms, nfqs, nongsaro,
oneclick-law, open-assembly, open-law, pqis, psis, q-net, safemap, safetydata, seogu, seoul-bus, seoul-open-data,
seogwipo, sexoffender, sisul, sisul-www, stcis, tour, uiryeong, ulsan, vworld, wamis, work, work24, and worldjob.

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
- `reports/data-go-kr/link-detail-registry-patches.json`
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
| `reports/data-go-kr/link-detail-registry-patches.json` | bounded `datapan catalog enrich link-details` output, `data/data-go-kr.registry.json`, `data/provider-index.json` | `scripts/validate-link-detail-registry-patches.py` validates that every materialized link-detail operation targets an already registered adapter host before registry mutation. |
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

The current operation materialization queue still starts at `울산항만공사`: `98`
APIs, `6` APIs with operation mappings, and `92` uncovered APIs. The July 2026
portal pages and catalog JSON for that queue return error/not-found responses,
so it should be tracked as a reactivation blocker while the next viable queue
is processed. The runtime reactivation queue starts at `행정안전부`: `1252`
APIs, `1252` APIs with
operation mappings, `1767` mapped operations, `96` checked runtime evidence
records, and `1202` APIs still needing runtime reactivation. Gateway calls need
a data.go.kr service key; no-key runs are useful only to prove parameter
readiness, not to advance verified runtime coverage.

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
    the generated overview/backlog artifacts. The first operation
    materialization queue is `울산항만공사`, while the first runtime
    reactivation queue is `행정안전부`, using institution-scoped
    `datapan catalog verify --org` batches after data.go.kr credentials are
    available.
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
   runtime evidence to `1231` and clearing the then-current unrounded `10%`
   runtime evidence target gap. Gira #97 adds `data-gg` external endpoint
   verification evidence,
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
   increasing registered external adapter operations to `813`; an adapter-safe
   link-detail enrichment batches then add 302 APIs and 633 operations across
   already registered external hosts, increasing registered external adapter
   operations to `1446` while keeping evidence-adjusted adapter candidates at
   `0`. This is still mostly skipped
   boundary evidence, and the new
   `data-gg`/`nfqs`/`nongsaro`/`gwanak`/`mafra`/`garak`/`work24`/
   `seoul-open-data`/`culture`/`happysd`/`ncpms`/`i815` results prove landing-page
   reachability rather than generic machine-call support; the Safety Data
   operations remain approval-gated until credentials and approval state are
   available. After that operation materialization growth, culture, data-gg,
   and mafra external endpoint batches add `25` verified landing-page checks.
   The Safety Map adapter then registers `www.safemap.go.kr`, materializes the
   next 행정안전부 link-detail batch, and adds `10` verified safemap landing-page
   checks, bringing checked runtime evidence to `1313`, verified results to
   `104`, and keeping the `10%` release-readiness target restored. The next
   행정안전부 Safety Data batch adds `97` operation mappings and `10`
   approval-gated bounded checks, bringing checked runtime evidence to `1323`
   and registered external adapter operations to `1543`. The EShare adapter
   then registers `www.eshare.go.kr`, materializes the next 행정안전부 link-detail
   batch with `96` APIs and `121` operations, and adds `10` verified EShare
   landing-page checks, bringing checked runtime evidence to `1333` and
   registered external adapter operations to `1664`. The Lofin365 adapter then
   registers `www.lofin365.go.kr`, materializes another 행정안전부 batch with
   `95` APIs and `239` operations, and adds `25` failed-but-bounded Lofin365
   runtime checks, bringing checked runtime evidence to `1358` and registered
   external adapter operations to `1903`. The Juso adapter then registers
   `www.juso.go.kr`, materializes `68` of the last `73` 행정안전부 APIs with
   `144` operations, and adds `10` verified Juso landing-page checks, bringing
   checked runtime evidence to `1368` and registered external adapter
   operations to `2047`. The remaining 행정안전부 link-detail hosts then add
   Food Safety Korea, INS24, Jeju Data Hub, and VWorld adapters plus the bare
   Seoul Open Data API host, opening the final `5` 행정안전부 APIs, adding `9`
   operations, and merging `7` bounded checks. That brings checked runtime
   evidence to `1375` and registered external adapter operations to `2056`.
   The first 경기도 batch then adds Gwangmyeong and Seogu adapters, materializes
   `100` APIs and `291` operations, and merges `31` bounded checks. That brings
   checked runtime evidence to `1406` and registered external adapter operations
   to `2347`. The second 경기도 batch then adds the DGFCA adapter, materializes
   another `100` APIs and `282` operations, and merges `21` bounded checks.
   That brings checked runtime evidence to `1427` and registered external
   adapter operations to `2629`. The third through fifth 경기도 batches add
   WAMIS, Gimhae, Jeju Air, Open Law, Dongjak, and Korea Expressway coverage,
   materialize `300` APIs and `792` operations, and merge `79` bounded checks,
   bringing checked runtime evidence to `1506` and registered external adapter
   operations to `3421`. The sixth 경기도 batch adds Work, Icheon, Sisul WWW,
   and KISTEP verification adapters, materializes another `100` APIs and `252`
   operations, and merges `24` bounded checks. That brings checked runtime
   evidence to `1530` and registered external adapter operations to `3673`.
   The seventh 경기도 batch closes the remaining `41` 경기도 APIs with `111`
   operations and merges `11` verified bounded checks, bringing checked runtime
   evidence to `1541`, registered external adapter operations to `3784`, and
   moving the first materialization queue to 국토교통부. The first 국토교통부 batch
   adds Calspia, Car365, and Codil verification adapters, materializes `100`
   APIs and `260` operations, and merges `26` bounded checks. That brings
   checked runtime evidence to `1567`, registered external adapter operations
   to `4044`, and raises 국토교통부 operation coverage to `227` of `393` APIs.
   The second 국토교통부 batch adds Car, ITS, Jeju WWW, and KOFPI verification
   adapters, materializes another `100` APIs and `221` operations, and merges
   `22` bounded checks. That brings checked runtime evidence to `1589`,
   registered external adapter operations to `4265`, and raises 국토교통부
   operation coverage to `327` of `393` APIs.
   The final 국토교통부 batch extends ITS host coverage, adds the STCIS
   verification adapter, materializes the remaining `66` APIs and `177`
   operations, and merges `18` verified bounded checks. That brings checked
   runtime evidence to `1607`, registered external adapter operations to
   `4442`, completes 국토교통부 materialization at `393` of `393` APIs, and moves
   the first materialization queue to 식품의약품안전처.
   The first 식품의약품안전처 batch materializes `100` APIs and `234` operations
   without needing new adapters, and merges `23` verified bounded checks. That
   brings checked runtime evidence to `1630`, registered external adapter
   operations to `4676`, and raises 식품의약품안전처 operation coverage to `372`
   of `392` APIs.
   The final 식품의약품안전처 batch materializes the remaining `20` APIs and `51`
   operations without needing new adapters, and merges `30` skipped bounded
   checks for missing required parameters. That brings checked runtime evidence
   to `1660`, registered external adapter operations to `4727`, completes
   식품의약품안전처 materialization at `392` of `392` APIs, and moves the first
   materialization queue to 국회 국회사무처.
   The first 국회 국회사무처 batch adds the Open Assembly verification adapter,
   materializes `100` APIs and `100` operations, and merges `10`
   failed-but-bounded HTTP 400 checks. That brings checked runtime evidence to
   `1670`, registered external adapter operations to `4827`, and raises 국회
   국회사무처 operation coverage to `100` of `277` APIs.
   The second 국회 국회사무처 batch materializes another `100` APIs and `100`
   operations, and merges `10` more failed-but-bounded HTTP 400 checks. That
   brings checked runtime evidence to `1680`, registered external adapter
   operations to `4927`, and raises 국회 국회사무처 operation coverage to `200`
   of `277` APIs.
   The final 국회 국회사무처 batch materializes the remaining `77` APIs and `77`
   operations, and merges `10` more failed-but-bounded HTTP 400 checks. That
   brings checked runtime evidence to `1690`, registered external adapter
   operations to `5004`, completes 국회 국회사무처 materialization at `277` of
   `277` APIs, and moves the first materialization queue to 성평등가족부.
   The final 성평등가족부 batch adds the Sex Offender verification adapter,
   materializes the remaining `1` API and `1` operation, and merges `1`
   verified landing-page check. That brings checked runtime evidence to `1691`,
   registered external adapter operations to `5005`, completes 성평등가족부
   materialization at `273` of `273` APIs, and moves the first materialization
   queue to 공정거래위원회.
   The 공정거래위원회 batch adds Consumer, Fair Data, and Franchise FTC
   verification adapters, materializes the remaining `34` APIs and `58`
   operations, and merges `23` verified landing-page checks. That brings checked
   runtime evidence to `1714`, registered external adapter operations to
   `5063`, completes 공정거래위원회 materialization at `250` of `250` APIs, and
   moves the first materialization queue to 한국산업인력공단.
   The 한국산업인력공단 batch adds WorldJob adapter coverage, materializes the
   remaining `2` APIs and `2` operations, and merges `2` verified landing-page
   checks. That brings checked runtime evidence to `1716`, registered external
   adapter operations to `5065`, completes 한국산업인력공단 materialization at
   `230` of `230` APIs, and moves the first materialization queue to 국립암센터.
   The 국립암센터 batch adds Cancer adapter coverage, materializes the remaining
   `8` APIs and `8` operations, and merges `8` verified landing-page checks.
   That brings checked runtime evidence to `1724`, registered external adapter
   operations to `5073`, completes 국립암센터 materialization at `212` of `212`
   APIs, and moves the first materialization queue to 법제처.
   The first 법제처 batch extends Open Law host coverage to `www.law.go.kr` and
   `www.lawmaking.go.kr`, materializes `100` APIs and `161` operations, and
   merges `25` verified landing-page checks. That brings checked runtime
   evidence to `1749`, registered external adapter operations to `5234`, and
   raises 법제처 operation coverage to `105` of `203` APIs.
   The final 법제처 batch materializes the remaining `98` APIs and `204`
   operations, and merges `25` verified Open Law landing-page checks. That
   brings checked runtime evidence to `1774`, registered external adapter
   operations to `5438`, completes 법제처 materialization at `203` of `203`
   APIs, and moves the first materialization queue to 경기도 광명시.
   The first 경기도 광명시 batch materializes `100` APIs and `290` operations
   using existing Gwangmyeong adapter coverage, and merges `25` verified
   landing-page checks. That brings checked runtime evidence to `1799`,
   registered external adapter operations to `5728`, and raises 경기도 광명시
   operation coverage to `100` of `197` APIs.
   The final 경기도 광명시 batch materializes the remaining `97` APIs and `288`
   operations using existing Gwangmyeong adapter coverage, and merges `25`
   verified landing-page checks. That brings checked runtime evidence to
   `1824`, registered external adapter operations to `6016`, completes 경기도
   광명시 materialization at `197` of `197` APIs, and moves the first
   materialization queue to 해양수산부.
   The 해양수산부 GICOMS batch adds `www.gicoms.go.kr` adapter coverage,
   materializes the remaining `1` API and `1` operation, and merges `1`
   failed-but-bounded timeout check. That brings checked runtime evidence to
   `1825`, registered external adapter operations to `6017`, completes
   해양수산부 materialization at `173` of `173` APIs, and moves the first
   materialization queue to 제주특별자치도.
   The first 제주특별자치도 batch adds Gwangjin, Jeju ITS, MND Open Data, and
   Seogwipo adapter coverage, materializes `100` APIs and `221` operations,
   and merges `24` verified landing-page checks plus `1` failed bounded check.
   That brings checked runtime evidence to `1850`, registered external adapter
   operations to `6238`, and raises 제주특별자치도 operation coverage to `118`
   of `171` APIs.
   The final 제주특별자치도 batch materializes the remaining `53` APIs and `142`
   operations, and merges `25` verified landing-page checks. That brings
   checked runtime evidence to `1875`, registered external adapter operations
   to `6380`, completes 제주특별자치도 materialization at `171` of `171` APIs,
   and moves the first materialization queue to 농촌진흥청.
   The first 농촌진흥청 batch extends Nongsaro host coverage to bare
   `nongsaro.go.kr`, adds the PSIS verification adapter, materializes `100`
   APIs and `260` operations, and merges `21` verified landing-page checks
   plus `4` skipped parameter-blocked checks. That brings checked runtime
   evidence to `1900`, registered external adapter operations to `6640`, and
   raises 농촌진흥청 operation coverage to `114` of `136` APIs.
   The final 농촌진흥청 batch adds NABIC adapter coverage, materializes the
   remaining `22` APIs and `58` operations, and merges `25` verified
   landing-page checks. That brings checked runtime evidence to `1925`,
   registered external adapter operations to `6698`, completes 농촌진흥청
   materialization at `136` of `136` APIs, and moves the first materialization
   queue to 대전광역시 서구.
   The first 대전광역시 서구 batch extends HappySD host coverage to
   `parking.happysd.or.kr`, materializes `100` APIs and `139` operations, and
   merges `25` verified landing-page checks. That brings checked runtime
   evidence to `1950`, registered external adapter operations to `6837`, and
   raises 대전광역시 서구 operation coverage to `100` of `125` APIs.
   The final 대전광역시 서구 batch materializes the remaining `25` APIs and `32`
   operations, and merges `25` verified landing-page checks. That brings
   checked runtime evidence to `1975`, registered external adapter operations
   to `6869`, completes 대전광역시 서구 materialization at `125` of `125` APIs,
   and moves the first materialization queue to 전라남도.
   The final 전라남도 batch adds Jeonnam Redtable adapter coverage, materializes
   the remaining `1` API and `1` operation, and merges `1` verified landing-page
   check. That brings checked runtime evidence to `1976`, registered external
   adapter operations to `6870`, completes 전라남도 materialization at `109` of
   `109` APIs, and moves the first materialization queue to 울산항만공사.
   The next 충청남도 batch registers the Chungnam host family, materializes `27`
   APIs and `65` operations, and merges `65` failed-but-bounded runtime checks
   (`48` HTTP 404 and `17` HTTP 403). That brings checked runtime evidence to
   `2041`, registered external adapter operations to `6935`, raises API
   operation coverage to `10,226` of `12,060` APIs (`84.8%`), and leaves
   울산항만공사 as a blocked materialization/reactivation queue.
   The following 한국도로공사 batch materializes `93` APIs and `271`
   operations, and merges `271` verified `data.ex.co.kr`/related landing-page
   checks. That brings checked runtime evidence to `2312`, verified checks to
   `903`, registered external adapter operations to `7206`, and raises API
   operation coverage to `10,319` of `12,060` APIs (`85.6%`).
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
- runtime evidence coverage meets the current `10%` target without treating
  skipped boundary evidence as callable success;
- downstream impact plans can express `no_action`, `refresh_verification`,
  `update_adapter`, and `db_migration_review` for data.go.kr changes.
