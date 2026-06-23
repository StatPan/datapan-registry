# data.go.kr Release Provenance

- generated_at: 2026-06-23T23:17:24Z
- datapan_version: 0.1.0-dev
- source_provider: data.go.kr
- source_registry: .datapan\data-go-kr.registry.json
- release_registry: .datapan\release-epost-smoke\data\data-go-kr.registry.json
- provider_limit: 0

## Commands

```bash
datapan catalog release draft --registry .datapan\data-go-kr.registry.json --output-dir .datapan\release-epost-smoke --provider-limit 0 --json
# provider index: .datapan\release-epost-smoke\data\provider-index.json
datapan catalog audit --registry .datapan\release-epost-smoke\data\data-go-kr.registry.json --output .datapan\release-epost-smoke\reports\catalog-audit.json --json
datapan catalog errors --registry .datapan\release-epost-smoke\data\data-go-kr.registry.json --output .datapan\release-epost-smoke\reports\error-catalog.json --json
datapan catalog dependencies --registry .datapan\release-epost-smoke\data\data-go-kr.registry.json --limit 0 --output .datapan\release-epost-smoke\reports\dependencies.json --json
datapan catalog adapter-targets --registry .datapan\release-epost-smoke\data\data-go-kr.registry.json --limit 0 --output .datapan\release-epost-smoke\reports\adapter-targets.json --json
datapan catalog providers --registry .datapan\release-epost-smoke\data\data-go-kr.registry.json --limit 0 --output .datapan\release-epost-smoke\reports\provider-backlog.json --json
```
