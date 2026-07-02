# Datapan Registry Release

- generated_at: `2026-07-02T21:00:31Z`
- provider: `data.go.kr`
- datapan_version: `0.1.0-dev`
- source_registry: `C:\Users\statp\dev\datapan-registry\data\data-go-kr.registry.json`
- release_manifest: `manifest.json`

## Registry

- specs: `12060`
- catalog_diff: not included; no previous registry was provided

## Provider Coverage

- provider_adapters: `39` adapters, `44` hosts
- split_readiness: `ready`
- verification_capable_adapters: `39`
- call_capable_adapters: `23`
- dependency_operations: `12258` total, `11419` gateway, `648` external, `638` registered-adapter, `29` missing-adapter
- adapter_backlog: `11` target hosts, `29` target operations
- route_disposition: `29` routes, `14` dead-route candidates, `15` transient failures, `0` parameter-blocked, `0` adapter candidates
- route_disposition_artifact: `reports/route-disposition.json`
- provider_backlog: `193` hosts, `11` missing-adapter hosts, `29` operations needing adapters
- coverage: `12116` callable operations (`98.8%`), external adapter coverage `95.7%`, verification evidence coverage `10.4%`, evidence-adjusted adapter candidates `0`
- coverage_artifact: `reports/coverage.json`
- coverage_goals: callable `99%`, external adapters `98%`, verification evidence `10%`, call-capable adapters `25`, missing-adapter operations `<=10`
- verification_plan: `2` batches, `15` planned operations, `10774` gateway gaps, `5` adapter gaps
- verification_plan_artifact: `reports/verification-plan.json`
- runtime_evidence_growth: `10.4%` coverage, target `10.0%`, remaining `0`, status `above_target`
- runtime_evidence_growth_artifact: `reports/data-go-kr/runtime-evidence-growth.json`

Top adapter targets:

- `1`. `openapi.coast.kr`: `6` operations across `1` specs
- `2`. `www.rda.go.kr`: `4` operations across `2` specs
- `3`. `car.daegu.go.kr`: `4` operations across `1` specs
- `4`. `openapi.price.go.kr`: `4` operations across `1` specs
- `5`. `its.gyeongju.go.kr:81`: `3` operations across `1` specs

## Verification Evidence

- verification: `1278` total, `69` verified, `87` failed, `1122` skipped, `0` unknown
- verification_artifact: `reports/latest-verification.json`
- verification_summary_artifact: `reports/latest-verification-summary.json`

Provider evidence:

- `data.go.kr`: `645`
- `q-net`: `147`
- `jeonju`: `80`
- `ekape`: `49`
- `geoje`: `41`
- `uiryeong`: `40`

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
