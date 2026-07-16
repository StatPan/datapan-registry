# Datapan Registry Release

- generated_at: `2026-07-04T06:39:24Z`
- provider: `data.go.kr`
- datapan_version: `0.1.0-dev`
- source_registry: `data/data-go-kr.registry.json`
- previous_registry: `.datapan/previous/data-go-kr.registry.json`
- release_manifest: `manifest.json`

## Registry

- specs: `12060`
- catalog_diff: `0` added, `0` removed, `4402` changed, `7658` stable
- catalog_diff_artifact: `reports/catalog-diff.json`

## Provider Coverage

- provider_adapters: `138` adapters, `173` hosts
- split_readiness: `ready`
- verification_capable_adapters: `138`
- call_capable_adapters: `23`
- dependency_operations: `21256` total, `11419` gateway, `9646` external, `9636` registered-adapter, `29` missing-adapter
- adapter_backlog: `11` target hosts, `29` target operations
- route_disposition: `29` routes, `14` dead-route candidates, `15` transient failures, `0` parameter-blocked, `0` adapter candidates
- route_disposition_artifact: `reports/route-disposition.json`
- provider_backlog: `326` hosts, `11` missing-adapter hosts, `29` operations needing adapters
- coverage: `21114` callable operations (`99.3%`), external adapter coverage `99.7%`, verification evidence coverage `22.5%`, evidence-adjusted adapter candidates `0`
- coverage_artifact: `reports/coverage.json`
- coverage_goals: callable `99%`, external adapters `98%`, verification evidence `10%`, call-capable adapters `25`, missing-adapter operations `<=10`
- verification_plan: `20` batches, `114` planned operations, `10714` gateway gaps, `3393` adapter gaps
- verification_plan_artifact: `reports/verification-plan.json`
- runtime_evidence_growth: `22.5%` coverage, target `10.0%`, remaining `0`, status `above_target`
- runtime_evidence_growth_artifact: `reports/data-go-kr/runtime-evidence-growth.json`

Top adapter targets:

- `1`. `openapi.coast.kr`: `6` operations across `1` specs
- `2`. `www.rda.go.kr`: `4` operations across `2` specs
- `3`. `car.daegu.go.kr`: `4` operations across `1` specs
- `4`. `openapi.price.go.kr`: `4` operations across `1` specs
- `5`. `its.gyeongju.go.kr:81`: `3` operations across `1` specs

## Verification Evidence

- verification: `4774` total, `2841` verified, `389` failed, `1544` skipped, `0` unknown
- verification_artifact: `reports/latest-verification.json`
- verification_summary_artifact: `reports/latest-verification-summary.json`

Provider evidence:

- `data.go.kr`: `715`
- `mafra`: `322`
- `ex`: `277`
- `opendart`: `245`
- `culture`: `178`
- `seoul-open-data`: `156`

- unadapted_external_probe: `81` total, `0` verified, `29` failed, `0` skipped, `52` unknown
- unadapted_external_probe_artifact: `reports/unadapted-external-probe.json`
- unadapted_external_probe_summary_artifact: `reports/unadapted-external-probe-summary.json`

Unadapted external probe reasons:

- `unadapted_probe_http_2xx`: `52`
- `unadapted_probe_http_404`: `14`
- `unadapted_probe_timeout`: `8`
- `unadapted_probe_dns`: `6`
- `unadapted_probe_request_error`: `1`

## Publication Checks

```bash
datapan catalog release verify --manifest manifest.json --output reports/latest-release-verification.json --json
datapan catalog release readiness --manifest manifest.json --output reports/latest-release-readiness.json --json
```

## Diagnostic contract v1 (prepared, not yet published)

This release change prepares the stable diagnostic envelope schema, bounded
cause/action vocabulary, data.go.kr evidence mapping, deterministic examples,
and compatibility evidence for Datapan CLI, Healthcheck, and Web. Every public
artifact is bound by `manifest.json`; the schema is additionally bound by
`schemas/index.json`. `reports/diagnostic-publication-readiness.json` pins the
accepted candidate binding and #571 merge commit while explicitly denying
publishing, runtime, live-history, and consumer-deployment authority.

Registry owns immutable diagnostic facts, vocabulary, evidence shapes, and
artifact identity. Consumers own live inference, presentation, user-specific
state, and mutable history. Unknown cause/action IDs must fall back to
`unknown` / `gather_more_evidence`; existing v1 identifiers cannot be removed,
redefined, or extended in place.

Tagging, GitHub Release creation, Hugging Face publication, and Web rollout are
separate post-merge gates. After an explicit publication, operators must run
anonymous `verify-remote` with the full nonzero payload revision and require
the exact manifest-bound diagnostic paths and SHA-256 values. Datapan Web may
adopt the immutable artifacts only after that proof succeeds.

The workflow's automatic expected-revision receipt verifies baseline immutable
distribution integrity but does not supply the checked-in required PATH/SHA-256
set. It therefore does not replace or satisfy the separate post-public Web
adoption gate.
