# Datapan Registry Release

- generated_at: `2026-06-25T00:44:14Z`
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

- provider_adapters: `12` adapters, `15` hosts
- split_readiness: `ready`
- verification_capable_adapters: `12`
- call_capable_adapters: `7`
- dependency_operations: `12205` total, `11419` gateway, `594` external, `453` registered-adapter, `160` missing-adapter
- adapter_backlog: `25` target hosts, `160` target operations
- provider_backlog: `179` hosts, `25` missing-adapter hosts, `141` operations needing adapters
- coverage: `12063` callable operations (`98.8%`), external adapter coverage `73.9%`, verification evidence coverage `1.0%`
- coverage_artifact: `reports/coverage.json`
- verification_plan: `8` batches, `80` planned operations, `11409` gateway gaps, `337` adapter gaps
- verification_plan_artifact: `reports/verification-plan.json`

Top adapter targets:

- `1`. `oneclick.law.go.kr:80`: `27` operations across `3` specs
- `2`. `openapi.tour.go.kr`: `26` operations across `7` specs
- `3`. `www.korad.or.kr`: `15` operations across `13` specs
- `4`. `open.itfind.or.kr`: `13` operations across `5` specs
- `5`. `data.naqs.go.kr`: `9` operations across `2` specs

## Verification Evidence

- verification: `126` total, `16` verified, `40` failed, `70` skipped, `0` unknown
- verification_artifact: `reports/latest-verification.json`
- verification_summary_artifact: `reports/latest-verification-summary.json`

Provider evidence:

- `sisul`: `20`
- `andong`: `15`
- `ekape`: `15`
- `epost`: `15`
- `q-net`: `15`
- `data.go.kr`: `10`

## Publication Checks

```bash
datapan catalog release verify --manifest manifest.json --output reports/latest-release-verification.json --json
datapan catalog release readiness --manifest manifest.json --output reports/latest-release-readiness.json --json
```
