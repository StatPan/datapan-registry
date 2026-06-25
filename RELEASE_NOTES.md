# Datapan Registry Release

- generated_at: `2026-06-25T01:04:08Z`
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

- provider_adapters: `13` adapters, `16` hosts
- split_readiness: `ready`
- verification_capable_adapters: `13`
- call_capable_adapters: `8`
- dependency_operations: `12205` total, `11419` gateway, `594` external, `466` registered-adapter, `147` missing-adapter
- adapter_backlog: `24` target hosts, `147` target operations
- provider_backlog: `179` hosts, `24` missing-adapter hosts, `128` operations needing adapters
- coverage: `12063` callable operations (`98.8%`), external adapter coverage `76.0%`, verification evidence coverage `1.1%`
- coverage_artifact: `reports/coverage.json`
- verification_plan: `8` batches, `80` planned operations, `11409` gateway gaps, `337` adapter gaps
- verification_plan_artifact: `reports/verification-plan.json`

Top adapter targets:

- `1`. `oneclick.law.go.kr:80`: `27` operations across `3` specs
- `2`. `openapi.tour.go.kr`: `26` operations across `7` specs
- `3`. `www.korad.or.kr`: `15` operations across `13` specs
- `4`. `data.naqs.go.kr`: `9` operations across `2` specs
- `5`. `data.humetro.busan.kr`: `8` operations across `8` specs

## Verification Evidence

- verification: `139` total, `20` verified, `43` failed, `76` skipped, `0` unknown
- verification_artifact: `reports/latest-verification.json`
- verification_summary_artifact: `reports/latest-verification-summary.json`

Provider evidence:

- `sisul`: `20`
- `andong`: `15`
- `ekape`: `15`
- `epost`: `15`
- `q-net`: `15`
- `itfind`: `13`

## Publication Checks

```bash
datapan catalog release verify --manifest manifest.json --output reports/latest-release-verification.json --json
datapan catalog release readiness --manifest manifest.json --output reports/latest-release-readiness.json --json
```
