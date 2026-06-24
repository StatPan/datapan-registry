# data.go.kr Release Provenance

- generated_at: 2026-06-24T00:25:30Z
- datapan_version: 0.1.0-dev
- source_provider: data.go.kr
- source_registry: C:\workspace\datapan-registry\data\data-go-kr.registry.json
- release_registry: C:\workspace\datapan-registry\.datapan\release-with-evidence\data\data-go-kr.registry.json
- provider_limit: 0
- verification_source: C:\workspace\datapan-registry\reports\latest-verification.json

## Commands

```bash
datapan catalog release draft --registry C:\workspace\datapan-registry\data\data-go-kr.registry.json --output-dir C:\workspace\datapan-registry\.datapan\release-with-evidence --provider-limit 0 --verification C:\workspace\datapan-registry\reports\latest-verification.json --json
# provider index: C:\workspace\datapan-registry\.datapan\release-with-evidence\data\provider-index.json
datapan catalog audit --registry C:\workspace\datapan-registry\.datapan\release-with-evidence\data\data-go-kr.registry.json --output C:\workspace\datapan-registry\.datapan\release-with-evidence\reports\catalog-audit.json --json
datapan catalog errors --registry C:\workspace\datapan-registry\.datapan\release-with-evidence\data\data-go-kr.registry.json --output C:\workspace\datapan-registry\.datapan\release-with-evidence\reports\error-catalog.json --json
datapan catalog dependencies --registry C:\workspace\datapan-registry\.datapan\release-with-evidence\data\data-go-kr.registry.json --limit 0 --output C:\workspace\datapan-registry\.datapan\release-with-evidence\reports\dependencies.json --json
datapan catalog adapter-targets --registry C:\workspace\datapan-registry\.datapan\release-with-evidence\data\data-go-kr.registry.json --limit 0 --output C:\workspace\datapan-registry\.datapan\release-with-evidence\reports\adapter-targets.json --json
datapan catalog providers --registry C:\workspace\datapan-registry\.datapan\release-with-evidence\data\data-go-kr.registry.json --limit 0 --output C:\workspace\datapan-registry\.datapan\release-with-evidence\reports\provider-backlog.json --json
datapan catalog verify --input C:\workspace\datapan-registry\.datapan\release-with-evidence\reports\latest-verification.json --json
datapan catalog verify summary --input C:\workspace\datapan-registry\.datapan\release-with-evidence\reports\latest-verification.json --output C:\workspace\datapan-registry\.datapan\release-with-evidence\reports\latest-verification-summary.json --json
```
