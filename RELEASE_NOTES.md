# Datapan Registry Release

- generated_at: `2026-07-03T16:56:39Z`
- provider: `data.go.kr`
- datapan_version: `0.1.0-dev`
- source_registry: `data\data-go-kr.registry.json`
- previous_registry: `.datapan\previous-data-go-kr.registry.json`
- release_manifest: `manifest.json`

## Registry

- specs: `12060`
- catalog_diff: `0` added, `0` removed, `8` changed, `12052` stable
- catalog_diff_artifact: `reports/catalog-diff.json`

## Provider Coverage

- provider_adapters: `75` adapters, `83` hosts
- split_readiness: `ready`
- verification_capable_adapters: `75`
- call_capable_adapters: `23`
- dependency_operations: `16693` total, `11419` gateway, `5083` external, `5073` registered-adapter, `29` missing-adapter
- adapter_backlog: `11` target hosts, `29` target operations
- route_disposition: `29` routes, `14` dead-route candidates, `15` transient failures, `0` parameter-blocked, `0` adapter candidates
- route_disposition_artifact: `reports/route-disposition.json`
- provider_backlog: `234` hosts, `11` missing-adapter hosts, `29` operations needing adapters
- coverage: `16551` callable operations (`99.1%`), external adapter coverage `99.4%`, verification evidence coverage `10.3%`, evidence-adjusted adapter candidates `0`
- coverage_artifact: `reports/coverage.json`
- coverage_goals: callable `99%`, external adapters `98%`, verification evidence `10%`, call-capable adapters `25`, missing-adapter operations `<=10`
- verification_plan: `20` batches, `142` planned operations, `10714` gateway gaps, `3579` adapter gaps
- verification_plan_artifact: `reports/verification-plan.json`
- runtime_evidence_growth: `10.3%` coverage, target `10.0%`, remaining `0`, status `above_target`
- runtime_evidence_growth_artifact: `reports/data-go-kr/runtime-evidence-growth.json`

Top adapter targets:

- `1`. `openapi.coast.kr`: `6` operations across `1` specs
- `2`. `www.rda.go.kr`: `4` operations across `2` specs
- `3`. `car.daegu.go.kr`: `4` operations across `1` specs
- `4`. `openapi.price.go.kr`: `4` operations across `1` specs
- `5`. `its.gyeongju.go.kr:81`: `3` operations across `1` specs

## Verification Evidence

- verification: `1724` total, `386` verified, `145` failed, `1193` skipped, `0` unknown
- verification_artifact: `reports/latest-verification.json`
- verification_summary_artifact: `reports/latest-verification-summary.json`

Provider evidence:

- `data.go.kr`: `705`
- `q-net`: `147`
- `data-gg`: `116`
- `jeonju`: `80`
- `ekape`: `49`
- `geoje`: `41`

- unadapted_external_probe: `81` total, `0` verified, `29` failed, `0` skipped, `52` unknown
- unadapted_external_probe_artifact: `reports/unadapted-external-probe.json`
- unadapted_external_probe_summary_artifact: `reports/unadapted-external-probe-summary.json`

Unadapted external probe reasons:

- `unadapted_probe_http_2xx`: `52`
- `unadapted_probe_http_404`: `14`
- `unadapted_probe_timeout`: `8`
- `unadapted_probe_dns`: `6`
- `unadapted_probe_request_error`: `1`

## Publication Checks

```bash
datapan catalog release verify --manifest manifest.json --output reports/latest-release-verification.json --json
datapan catalog release readiness --manifest manifest.json --output reports/latest-release-readiness.json --json
```
