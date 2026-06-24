# Datapan Registry Release

- generated_at: `2026-06-24T15:19:43Z`
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

- provider_adapters: `7` adapters, `10` hosts
- split_readiness: `ready`
- verification_capable_adapters: `7`
- call_capable_adapters: `2`
- dependency_operations: `12205` total, `11419` gateway, `594` external, `317` registered-adapter, `296` missing-adapter
- adapter_backlog: `30` target hosts, `296` target operations
- provider_backlog: `179` hosts, `30` missing-adapter hosts, `277` operations needing adapters
- coverage: `12063` callable operations (`98.8%`), external adapter coverage `51.7%`, verification evidence coverage `0.6%`
- coverage_artifact: `reports/coverage.json`
- verification_plan: `5` batches, `50` planned operations, `11409` gateway gaps, `254` adapter gaps
- verification_plan_artifact: `reports/verification-plan.json`

Top adapter targets:

- `1`. `data.geoje.go.kr`: `41` operations across `12` specs
- `2`. `data.uiryeong.go.kr`: `40` operations across `32` specs
- `3`. `oneclick.law.go.kr:80`: `27` operations across `3` specs
- `4`. `openapi.tour.go.kr`: `26` operations across `7` specs
- `5`. `data.sisul.or.kr`: `20` operations across `20` specs

## Verification Evidence

- verification: `73` total, `14` verified, `15` failed, `44` skipped, `0` unknown
- verification_artifact: `reports/latest-verification.json`
- verification_summary_artifact: `reports/latest-verification-summary.json`

Provider evidence:

- `ekape`: `15`
- `epost`: `15`
- `q-net`: `15`
- `data.go.kr`: `10`
- `airport`: `6`
- `jeonju`: `5`

## Publication Checks

```bash
datapan catalog release verify --manifest manifest.json --output reports/latest-release-verification.json --json
datapan catalog release readiness --manifest manifest.json --output reports/latest-release-readiness.json --json
```
