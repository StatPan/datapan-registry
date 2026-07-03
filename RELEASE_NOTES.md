# Datapan Registry Release

- generated_at: `2026-07-03T18:58:36Z`
- provider: `data.go.kr`
- datapan_version: `0.1.0-dev`
- source_registry: `data\data-go-kr.registry.json`
- previous_registry: `.datapan\previous-data-go-kr.registry.json`
- release_manifest: `manifest.json`

## Registry

- specs: `12060`
- catalog_diff: `0` added, `0` removed, `1` changed, `12059` stable
- catalog_diff_artifact: `reports/catalog-diff.json`

## Provider Coverage

- provider_adapters: `83` adapters, `95` hosts
- split_readiness: `ready`
- verification_capable_adapters: `83`
- call_capable_adapters: `23`
- dependency_operations: `18490` total, `11419` gateway, `6880` external, `6870` registered-adapter, `29` missing-adapter
- adapter_backlog: `11` target hosts, `29` target operations
- route_disposition: `29` routes, `14` dead-route candidates, `15` transient failures, `0` parameter-blocked, `0` adapter candidates
- route_disposition_artifact: `reports/route-disposition.json`
- provider_backlog: `247` hosts, `11` missing-adapter hosts, `29` operations needing adapters
- coverage: `18348` callable operations (`99.2%`), external adapter coverage `99.6%`, verification evidence coverage `10.7%`, evidence-adjusted adapter candidates `0`
- coverage_artifact: `reports/coverage.json`
- coverage_goals: callable `99%`, external adapters `98%`, verification evidence `10%`, call-capable adapters `25`, missing-adapter operations `<=10`
- verification_plan: `20` batches, `129` planned operations, `10714` gateway gaps, `3704` adapter gaps
- verification_plan_artifact: `reports/verification-plan.json`
- runtime_evidence_growth: `10.7%` coverage, target `10.0%`, remaining `0`, status `above_target`
- runtime_evidence_growth_artifact: `reports/data-go-kr/runtime-evidence-growth.json`

Top adapter targets:

- `1`. `openapi.coast.kr`: `6` operations across `1` specs
- `2`. `www.rda.go.kr`: `4` operations across `2` specs
- `3`. `car.daegu.go.kr`: `4` operations across `1` specs
- `4`. `openapi.price.go.kr`: `4` operations across `1` specs
- `5`. `its.gyeongju.go.kr:81`: `3` operations across `1` specs

## Verification Evidence

- verification: `1976` total, `632` verified, `147` failed, `1197` skipped, `0` unknown
- verification_artifact: `reports/latest-verification.json`
- verification_summary_artifact: `reports/latest-verification-summary.json`

Provider evidence:

- `data.go.kr`: `709`
- `q-net`: `147`
- `data-gg`: `117`
- `jeonju`: `80`
- `gwangmyeong`: `70`
- `open-law`: `51`

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
