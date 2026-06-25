# Datapan Registry Release

- generated_at: `2026-06-25T03:20:56Z`
- provider: `data.go.kr`
- datapan_version: `0.1.0-dev`
- source_registry: `C:\workspace\datapan-registry\data\data-go-kr.registry.json`
- previous_registry: `C:\workspace\datapan-registry\.datapan\previous\data-go-kr.registry.json`
- release_manifest: `manifest.json`

## Registry

- specs: `12060`
- catalog_diff: `0` added, `0` removed, `0` changed, `12060` stable
- catalog_diff_artifact: `reports/catalog-diff.json`

## Provider Coverage

- provider_adapters: `21` adapters, `25` hosts
- split_readiness: `ready`
- verification_capable_adapters: `21`
- call_capable_adapters: `16`
- dependency_operations: `12205` total, `11419` gateway, `594` external, `568` registered-adapter, `45` missing-adapter
- adapter_backlog: `15` target hosts, `45` target operations
- provider_backlog: `179` hosts, `15` missing-adapter hosts, `45` operations needing adapters
- coverage: `12063` callable operations (`98.8%`), external adapter coverage `92.7%`, verification evidence coverage `2.0%`
- coverage_artifact: `reports/coverage.json`
- verification_plan: `8` batches, `80` planned operations, `11409` gateway gaps, `337` adapter gaps
- verification_plan_artifact: `reports/verification-plan.json`

Top adapter targets:

- `1`. `openapi.coast.kr`: `6` operations across `1` specs
- `2`. `openapi.kpx.or.kr`: `5` operations across `5` specs
- `3`. `data.jeju.go.kr`: `4` operations across `2` specs
- `4`. `www.rda.go.kr`: `4` operations across `2` specs
- `5`. `car.daegu.go.kr`: `4` operations across `1` specs

## Verification Evidence

- verification: `241` total, `21` verified, `81` failed, `139` skipped, `0` unknown
- verification_artifact: `reports/latest-verification.json`
- verification_summary_artifact: `reports/latest-verification-summary.json`

Provider evidence:

- `oneclick-law`: `30`
- `tour`: `26`
- `sisul`: `20`
- `andong`: `15`
- `ekape`: `15`
- `epost`: `15`

## Publication Checks

```bash
datapan catalog release verify --manifest manifest.json --output reports/latest-release-verification.json --json
datapan catalog release readiness --manifest manifest.json --output reports/latest-release-readiness.json --json
```
