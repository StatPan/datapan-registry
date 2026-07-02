# Datapan Registry Release

- generated_at: `2026-07-02T16:06:15Z`
- provider: `data.go.kr`
- datapan_version: `0.1.0-dev`
- source_registry: `..\datapan-registry\data\data-go-kr.registry.json`
- previous_registry: `..\datapan-registry\.datapan\previous\data-go-kr.registry.json`
- release_manifest: `manifest.json`

## Registry

- specs: `12060`
- catalog_diff: `0` added, `0` removed, `25` changed, `12035` stable
- catalog_diff_artifact: `reports/catalog-diff.json`

## Provider Coverage

- provider_adapters: `26` adapters, `30` hosts
- split_readiness: `ready`
- verification_capable_adapters: `26`
- call_capable_adapters: `21`
- dependency_operations: `12253` total, `11419` gateway, `643` external, `586` registered-adapter, `76` missing-adapter
- adapter_backlog: `23` target hosts, `76` target operations
- route_disposition: `76` routes, `14` dead-route candidates, `15` transient failures, `0` parameter-blocked, `47` adapter candidates
- route_disposition_artifact: `reports/route-disposition.json`
- provider_backlog: `192` hosts, `23` missing-adapter hosts, `76` operations needing adapters
- coverage: `12111` callable operations (`98.8%`), external adapter coverage `88.5%`, verification evidence coverage `10.0%`, evidence-adjusted adapter candidates `47`
- coverage_artifact: `reports/coverage.json`
- coverage_goals: callable `99%`, external adapters `98%`, verification evidence `10%`, call-capable adapters `25`, missing-adapter operations `<=10`
- verification_plan: `1` batches, `10` planned operations, `10784` gateway gaps, `0` adapter gaps
- verification_plan_artifact: `reports/verification-plan.json`
- runtime_evidence_growth: `10.0%` coverage, target `10.0%`, remaining `5`, status `below_target`
- runtime_evidence_growth_artifact: `reports/data-go-kr/runtime-evidence-growth.json`
- runtime_evidence_warning: `warning` `runtime_evidence_below_target`

Top adapter targets:

- `1`. `data.gg.go.kr`: `18` operations across `8` specs
- `2`. `openapi.coast.kr`: `6` operations across `1` specs
- `3`. `www.nfqs.go.kr`: `5` operations across `3` specs
- `4`. `www.nongsaro.go.kr`: `4` operations across `2` specs
- `5`. `www.rda.go.kr`: `4` operations across `2` specs

## Verification Evidence

- verification: `1221` total, `22` verified, `87` failed, `1112` skipped, `0` unknown
- verification_artifact: `reports/latest-verification.json`
- verification_summary_artifact: `reports/latest-verification-summary.json`

Provider evidence:

- `data.go.kr`: `635`
- `q-net`: `147`
- `jeonju`: `80`
- `ekape`: `49`
- `geoje`: `41`
- `uiryeong`: `40`

- unadapted_external_probe: `76` total, `0` verified, `29` failed, `0` skipped, `47` unknown
- unadapted_external_probe_artifact: `reports/unadapted-external-probe.json`
- unadapted_external_probe_summary_artifact: `reports/unadapted-external-probe-summary.json`

Unadapted external probe reasons:

- `unadapted_probe_http_2xx`: `47`
- `unadapted_probe_http_404`: `14`
- `unadapted_probe_timeout`: `8`
- `unadapted_probe_dns`: `6`
- `unadapted_probe_request_error`: `1`

## Publication Checks

```bash
datapan catalog release verify --manifest manifest.json --output reports/latest-release-verification.json --json
datapan catalog release readiness --manifest manifest.json --output reports/latest-release-readiness.json --json
```
