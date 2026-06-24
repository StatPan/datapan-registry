# Datapan Registry Release

- generated_at: `2026-06-24T11:13:06Z`
- provider: `data.go.kr`
- datapan_version: `0.1.0-dev`
- source_registry: `data\data-go-kr.registry.json`
- previous_registry: `.datapan\previous\data-go-kr.registry.json`
- release_manifest: `manifest.json`

## Registry

- specs: `12060`
- catalog_diff: `0` added, `0` removed, `0` changed, `12060` stable
- catalog_diff_artifact: `reports/catalog-diff.json`

## Provider Coverage

- provider_adapters: `6` adapters, `9` hosts
- split_readiness: `not_ready`
- verification_capable_adapters: `6`
- call_capable_adapters: `0`
- dependency_operations: `12205` total, `11419` gateway, `594` external, `237` registered-adapter, `376` missing-adapter
- adapter_backlog: `31` target hosts, `376` target operations
- provider_backlog: `179` hosts, `31` missing-adapter hosts, `357` operations needing adapters

Top adapter targets:

- `1`. `openapi.jeonju.go.kr`: `80` operations across `49` specs
- `2`. `data.geoje.go.kr`: `41` operations across `12` specs
- `3`. `data.uiryeong.go.kr`: `40` operations across `32` specs
- `4`. `oneclick.law.go.kr:80`: `27` operations across `3` specs
- `5`. `openapi.tour.go.kr`: `26` operations across `7` specs

## Verification Evidence

- verification: `28` total, `6` verified, `9` failed, `13` skipped, `0` unknown
- verification_artifact: `reports/latest-verification.json`
- verification_summary_artifact: `reports/latest-verification-summary.json`

Provider evidence:

- `airport`: `6`
- `ekape`: `5`
- `epost`: `5`
- `q-net`: `5`
- `forest`: `4`
- `folk`: `3`

## Publication Checks

```bash
datapan catalog release verify --manifest manifest.json --output reports/latest-release-verification.json --json
datapan catalog release readiness --manifest manifest.json --output reports/latest-release-readiness.json --json
```
