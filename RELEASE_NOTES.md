# Datapan Registry Release

- generated_at: `2026-06-30T07:22:51Z`
- provider: `data.go.kr`
- datapan_version: `0.1.0-dev`
- source_registry: `data/data-go-kr.registry.json`
- previous_registry: `data/data-go-kr.registry.json`
- release_manifest: `manifest.json`

## Registry

- specs: `12060`
- catalog_diff: `0` added, `0` removed, `0` changed, `12060` stable
- catalog_diff_artifact: `reports/catalog-diff.json`

## Provider Coverage

- provider_adapters: `26` adapters, `30` hosts
- split_readiness: `ready`
- verification_capable_adapters: `26`
- call_capable_adapters: `21`
- dependency_operations: `12205` total, `11419` gateway, `595` external, `586` registered-adapter, `28` missing-adapter
- adapter_backlog: `10` target hosts, `28` target operations
- route_disposition: `28` routes, `14` dead-route candidates, `14` transient failures, `0` parameter-blocked, `0` adapter candidates
- route_disposition_artifact: `reports/route-disposition.json`
- provider_backlog: `179` hosts, `10` missing-adapter hosts, `28` operations needing adapters
- coverage: `12063` callable operations (`98.8%`), external adapter coverage `95.4%`, verification evidence coverage `5.5%`, evidence-adjusted adapter candidates `0`
- coverage_artifact: `reports/coverage.json`
- coverage_goals: callable `99%`, external adapters `98%`, verification evidence `10%`, call-capable adapters `25`, missing-adapter operations `<=10`
- verification_plan: `1` batches, `10` planned operations, `11339` gateway gaps, `0` adapter gaps
- verification_plan_artifact: `reports/verification-plan.json`
- runtime_evidence_growth: `5.5%` coverage, target `10.0%`, remaining `555`, status `below_target`
- runtime_evidence_growth_artifact: `reports/data-go-kr/runtime-evidence-growth.json`
- runtime_evidence_warning: `warning` `runtime_evidence_below_target`

Top adapter targets:

- `1`. `openapi.coast.kr`: `6` operations across `1` specs
- `2`. `www.rda.go.kr`: `4` operations across `2` specs
- `3`. `car.daegu.go.kr`: `4` operations across `1` specs
- `4`. `openapi.price.go.kr`: `4` operations across `1` specs
- `5`. `its.gyeongju.go.kr:81`: `3` operations across `1` specs

## Verification Evidence

- verification: `666` total, `22` verified, `87` failed, `557` skipped, `0` unknown
- verification_artifact: `reports/latest-verification.json`
- verification_summary_artifact: `reports/latest-verification-summary.json`

Provider evidence:

- `q-net`: `147`
- `data.go.kr`: `80`
- `jeonju`: `80`
- `ekape`: `49`
- `geoje`: `41`
- `uiryeong`: `40`

- unadapted_external_probe: `28` total, `0` verified, `28` failed, `0` skipped, `0` unknown
- unadapted_external_probe_artifact: `reports/unadapted-external-probe.json`
- unadapted_external_probe_summary_artifact: `reports/unadapted-external-probe-summary.json`

Unadapted external probe reasons:

- `unadapted_probe_http_404`: `14`
- `unadapted_probe_timeout`: `7`
- `unadapted_probe_request_error`: `6`
- `unadapted_probe_http_503`: `1`

## Publication Checks

```bash
datapan catalog release verify --manifest manifest.json --output reports/latest-release-verification.json --json
datapan catalog release readiness --manifest manifest.json --output reports/latest-release-readiness.json --json
```
