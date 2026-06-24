# data.go.kr Release Provenance

- generated_at: 2026-06-24T11:13:06Z
- datapan_version: 0.1.0-dev
- source_provider: data.go.kr
- source_registry: data\data-go-kr.registry.json
- previous_registry: .datapan\previous\data-go-kr.registry.json
- release_registry: .\data\data-go-kr.registry.json
- provider_limit: 0
- verification_source: reports\latest-verification.json

## Commands

```bash
datapan catalog release draft --registry data\data-go-kr.registry.json --output-dir . --provider-limit 0 --previous-registry .datapan\previous\data-go-kr.registry.json --verification reports\latest-verification.json --json
# provider index: .\data\provider-index.json
datapan catalog diff --old .datapan\previous\data-go-kr.registry.json --new .\data\data-go-kr.registry.json --limit 0 --output .\reports\catalog-diff.json --json
datapan catalog audit --registry .\data\data-go-kr.registry.json --output .\reports\catalog-audit.json --json
datapan catalog errors --registry .\data\data-go-kr.registry.json --output .\reports\error-catalog.json --json
datapan catalog dependencies --registry .\data\data-go-kr.registry.json --limit 0 --output .\reports\dependencies.json --json
datapan catalog adapter-targets --registry .\data\data-go-kr.registry.json --limit 0 --output .\reports\adapter-targets.json --json
datapan catalog providers --registry .\data\data-go-kr.registry.json --limit 0 --output .\reports\provider-backlog.json --json
datapan catalog verify --input .\reports\latest-verification.json --json
datapan catalog verify summary --input .\reports\latest-verification.json --output .\reports\latest-verification-summary.json --json
```
