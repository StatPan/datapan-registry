# data.go.kr Mastery Plan

`data.go.kr` is the baseline source for Datapan registry standardization. Before
adding broad multi-source coverage, this source must be mastered as the
reference implementation for coverage, evidence, external endpoint handling,
and downstream impact planning.

This plan uses the current release artifacts as the operating baseline.

## Baseline

Current release metrics:

- specs: `12060`
- operations: `12205`
- callable operations: `12063` (`98.8%`)
- data.go.kr gateway operations: `11419`
- external endpoint operations: `595`
- registered adapter operations: `586`
- missing adapter operations: `28`
- external adapter coverage: `95.4%`
- approval-required operations: `4142`
- no-endpoint operations: `123`
- service-root operations: `19`
- unsupported-protocol operations: `42`
- registered adapter hosts: `30`
- missing adapter hosts: `10`
- call-capable adapters: `21`

Current missing external route evidence:

- routes: `28`
- hosts: `10`
- with probe evidence: `28`
- dead-route candidates: `14`
- transient failures: `14`
- remaining adapter candidates: `0`

The practical interpretation is important: the remaining `28` missing external
routes should not automatically be treated as adapter backlog. They are covered
by manifest-bound probe and route-disposition evidence, and currently split
between dead-route candidates and transient failures.

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
`data/provider-index.json`: airport, andong, ekape, emuseum, epost, folk,
forest, gblib, geoje, humetro, itfind, jeju, jeonju, korad, kpx, lh-ebid,
myhome, naqs, oneclick-law, pqis, q-net, seoul-bus, sisul, tour, uiryeong, and
ulsan.

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
- `www.simpan.go.kr`: `1`

These hosts should not become adapter implementation tasks unless route
evidence changes from dead/transient to viable adapter candidate.

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
- `reports/latest-verification.json`
- `reports/latest-verification-summary.json`
- `reports/data-go-kr/error-action-catalog.json`
- future `reports/data-go-kr/registry-impact-plan.json`

## Source-Scoped Generation Contract

Checked-in data.go.kr source-scoped artifacts must name the root reports they
are generated from, then CI must verify that the source-scoped values still
match those roots.

| Source-scoped artifact | Required generation inputs | CI gate |
| --- | --- | --- |
| `reports/data-go-kr/external-coverage-summary.json` | `sources/data_go_kr.json`, `reports/coverage.json`, `reports/adapter-targets.json`, `reports/route-disposition.json`, `data/provider-index.json` | `scripts/validate-external-coverage.py` validates schema and cross-checks source identity, raw coverage metrics, route evidence counts, adapter target counts, provider-index host count, and missing host counts. |
| `reports/data-go-kr/error-action-catalog.json` | `sources/data_go_kr.json`, `reports/error-catalog.json`, `reports/route-disposition.json`, provider verification reports | `scripts/validate-error-action-catalogs.py` validates checked-in action rules; future generation should also fail on unmapped known error signatures. |
| `reports/data-go-kr/registry-impact-plan.json` | `sources/data_go_kr.json`, catalog diff, verification evidence, route disposition, error action catalog, promoted dataset mappings | Future `datapan-cli` generation must validate against `datapan.registry-impact-plan.v1` before client/server consumers act on it. |

This contract keeps `data/data-go-kr.registry.json` as the compatibility
registry path while moving generated evidence toward `reports/data-go-kr/`.
If any root report changes without the source-scoped artifact being refreshed,
CI should fail rather than treating the checked-in summary as authoritative.

## Task Sequence

1. Add and validate `sources/data_go_kr.json`. Done in PR #4.
2. Add source profile validation to CI. Done in PR #4.
3. Add a data.go.kr error action catalog draft. Done in PR #4.
4. Connect route-disposition reasons to error/action classifications. Started
   in `reports/data-go-kr/error-action-catalog.json`.
5. Add an evidence-adjusted external coverage summary. Done in PR #4 as a
   checked-in draft artifact.
6. Add source-scoped generation input cross-checks for data.go.kr external
   coverage. Done in PR #4.
7. Increase runtime verification evidence for call-capable registered adapters.
8. Generate a data.go.kr impact plan from catalog diff, verification evidence,
   route disposition, and promoted dataset mappings.

## Done Criteria

data.go.kr mastery is complete when:

- source profile validation is enforced in CI;
- gateway and external coverage are reported separately;
- evidence-adjusted adapter candidates are reported;
- every missing external route has route disposition evidence;
- known data.go.kr credential and approval failures map to error action rules;
- runtime evidence coverage is moving toward the `10%` target;
- downstream impact plans can express `no_action`, `refresh_verification`,
  `update_adapter`, and `db_migration_review` for data.go.kr changes.
