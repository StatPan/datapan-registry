# Datapan Registry Release

- generated_at: `2026-06-25T02:29:04Z`
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

- provider_adapters: `18` adapters, `22` hosts
- split_readiness: `ready`
- verification_capable_adapters: `18`
- call_capable_adapters: `13`
- dependency_operations: `12205` total, `11419` gateway, `594` external, `554` registered-adapter, `59` missing-adapter
- adapter_backlog: `18` target hosts, `59` target operations
- provider_backlog: `179` hosts, `18` missing-adapter hosts, `59` operations needing adapters
- coverage: `12063` callable operations (`98.8%`), external adapter coverage `90.4%`, verification evidence coverage `1.9%`
- coverage_artifact: `reports/coverage.json`
- verification_plan: `8` batches, `80` planned operations, `11409` gateway gaps, `337` adapter gaps
- verification_plan_artifact: `reports/verification-plan.json`

Top adapter targets:

- `1`. `openapi.ebid.lh.or.kr`: `6` operations across `6` specs
- `2`. `openapi.coast.kr`: `6` operations across `1` specs
- `3`. `openapi.kpx.or.kr`: `5` operations across `5` specs
- `4`. `ws.bus.go.kr`: `5` operations across `1` specs
- `5`. `data.jeju.go.kr`: `4` operations across `2` specs

## Verification Evidence

- verification: `227` total, `21` verified, `69` failed, `137` skipped, `0` unknown
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
