# Datapan Registry Release

- generated_at: `2026-06-24T22:23:56Z`
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

- provider_adapters: `8` adapters, `11` hosts
- split_readiness: `ready`
- verification_capable_adapters: `8`
- call_capable_adapters: `3`
- dependency_operations: `12205` total, `11419` gateway, `594` external, `358` registered-adapter, `255` missing-adapter
- adapter_backlog: `29` target hosts, `255` target operations
- provider_backlog: `179` hosts, `29` missing-adapter hosts, `236` operations needing adapters
- coverage: `12063` callable operations (`98.8%`), external adapter coverage `58.4%`, verification evidence coverage `0.6%`
- coverage_artifact: `reports/coverage.json`
- verification_plan: `6` batches, `60` planned operations, `11409` gateway gaps, `289` adapter gaps
- verification_plan_artifact: `reports/verification-plan.json`

Top adapter targets:

- `1`. `data.uiryeong.go.kr`: `40` operations across `32` specs
- `2`. `oneclick.law.go.kr:80`: `27` operations across `3` specs
- `3`. `openapi.tour.go.kr`: `26` operations across `7` specs
- `4`. `data.sisul.or.kr`: `20` operations across `20` specs
- `5`. `openapi.its.ulsan.kr`: `20` operations across `3` specs

## Verification Evidence

- verification: `79` total, `16` verified, `15` failed, `48` skipped, `0` unknown
- verification_artifact: `reports/latest-verification.json`
- verification_summary_artifact: `reports/latest-verification-summary.json`

Provider evidence:

- `ekape`: `15`
- `epost`: `15`
- `q-net`: `15`
- `data.go.kr`: `10`
- `airport`: `6`
- `geoje`: `6`

## Publication Checks

```bash
datapan catalog release verify --manifest manifest.json --output reports/latest-release-verification.json --json
datapan catalog release readiness --manifest manifest.json --output reports/latest-release-readiness.json --json
```
