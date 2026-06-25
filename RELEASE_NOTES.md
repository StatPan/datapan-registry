# Datapan Registry Release

- generated_at: `2026-06-25T02:06:16Z`
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

- provider_adapters: `17` adapters, `21` hosts
- split_readiness: `ready`
- verification_capable_adapters: `17`
- call_capable_adapters: `12`
- dependency_operations: `12205` total, `11419` gateway, `594` external, `528` registered-adapter, `85` missing-adapter
- adapter_backlog: `19` target hosts, `85` target operations
- provider_backlog: `179` hosts, `19` missing-adapter hosts, `66` operations needing adapters
- coverage: `12063` callable operations (`98.8%`), external adapter coverage `86.1%`, verification evidence coverage `1.6%`
- coverage_artifact: `reports/coverage.json`
- verification_plan: `8` batches, `80` planned operations, `11409` gateway gaps, `337` adapter gaps
- verification_plan_artifact: `reports/verification-plan.json`

Top adapter targets:

- `1`. `openapi.tour.go.kr`: `26` operations across `7` specs
- `2`. `openapi.ebid.lh.or.kr`: `6` operations across `6` specs
- `3`. `openapi.coast.kr`: `6` operations across `1` specs
- `4`. `openapi.kpx.or.kr`: `5` operations across `5` specs
- `5`. `ws.bus.go.kr`: `5` operations across `1` specs

## Verification Evidence

- verification: `201` total, `21` verified, `62` failed, `118` skipped, `0` unknown
- verification_artifact: `reports/latest-verification.json`
- verification_summary_artifact: `reports/latest-verification-summary.json`

Provider evidence:

- `oneclick-law`: `30`
- `sisul`: `20`
- `andong`: `15`
- `ekape`: `15`
- `epost`: `15`
- `korad`: `15`

## Publication Checks

```bash
datapan catalog release verify --manifest manifest.json --output reports/latest-release-verification.json --json
datapan catalog release readiness --manifest manifest.json --output reports/latest-release-readiness.json --json
```
