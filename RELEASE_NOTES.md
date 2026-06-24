# Datapan Registry Release

- generated_at: `2026-06-24T23:32:28Z`
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

- provider_adapters: `9` adapters, `12` hosts
- split_readiness: `ready`
- verification_capable_adapters: `9`
- call_capable_adapters: `4`
- dependency_operations: `12205` total, `11419` gateway, `594` external, `398` registered-adapter, `215` missing-adapter
- adapter_backlog: `28` target hosts, `215` target operations
- provider_backlog: `179` hosts, `28` missing-adapter hosts, `196` operations needing adapters
- coverage: `12063` callable operations (`98.8%`), external adapter coverage `64.9%`, verification evidence coverage `0.7%`
- coverage_artifact: `reports/coverage.json`
- verification_plan: `7` batches, `70` planned operations, `11409` gateway gaps, `323` adapter gaps
- verification_plan_artifact: `reports/verification-plan.json`

Top adapter targets:

- `1`. `oneclick.law.go.kr:80`: `27` operations across `3` specs
- `2`. `openapi.tour.go.kr`: `26` operations across `7` specs
- `3`. `data.sisul.or.kr`: `20` operations across `20` specs
- `4`. `openapi.its.ulsan.kr`: `20` operations across `3` specs
- `5`. `www.korad.or.kr`: `15` operations across `13` specs

## Verification Evidence

- verification: `85` total, `16` verified, `21` failed, `48` skipped, `0` unknown
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
