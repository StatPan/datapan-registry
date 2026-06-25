# data.go.kr Release Provenance

- generated_at: 2026-06-25T01:23:56Z
- datapan_version: 0.1.0-dev
- source_provider: data.go.kr
- source_registry: C:\workspace\datapan-registry\data\data-go-kr.registry.json
- previous_registry: C:\workspace\datapan-registry\.datapan\previous\data-go-kr.registry.json
- release_registry: C:\workspace\datapan-registry\.datapan\release-korad\data\data-go-kr.registry.json
- provider_limit: 0
- verification_source: C:\workspace\datapan-registry\reports\latest-verification.json

## Commands

```bash
datapan catalog release draft --registry C:\workspace\datapan-registry\data\data-go-kr.registry.json --output-dir C:\workspace\datapan-registry\.datapan\release-korad --provider-limit 0 --previous-registry C:\workspace\datapan-registry\.datapan\previous\data-go-kr.registry.json --verification C:\workspace\datapan-registry\reports\latest-verification.json --json
# provider index: C:\workspace\datapan-registry\.datapan\release-korad\data\provider-index.json
datapan catalog diff --old C:\workspace\datapan-registry\.datapan\previous\data-go-kr.registry.json --new C:\workspace\datapan-registry\.datapan\release-korad\data\data-go-kr.registry.json --limit 0 --output C:\workspace\datapan-registry\.datapan\release-korad\reports\catalog-diff.json --json
datapan catalog audit --registry C:\workspace\datapan-registry\.datapan\release-korad\data\data-go-kr.registry.json --output C:\workspace\datapan-registry\.datapan\release-korad\reports\catalog-audit.json --json
datapan catalog errors --registry C:\workspace\datapan-registry\.datapan\release-korad\data\data-go-kr.registry.json --output C:\workspace\datapan-registry\.datapan\release-korad\reports\error-catalog.json --json
datapan catalog dependencies --registry C:\workspace\datapan-registry\.datapan\release-korad\data\data-go-kr.registry.json --limit 0 --output C:\workspace\datapan-registry\.datapan\release-korad\reports\dependencies.json --json
datapan catalog adapter-targets --registry C:\workspace\datapan-registry\.datapan\release-korad\data\data-go-kr.registry.json --limit 0 --output C:\workspace\datapan-registry\.datapan\release-korad\reports\adapter-targets.json --json
datapan catalog providers --registry C:\workspace\datapan-registry\.datapan\release-korad\data\data-go-kr.registry.json --limit 0 --output C:\workspace\datapan-registry\.datapan\release-korad\reports\provider-backlog.json --json
datapan catalog verify --input C:\workspace\datapan-registry\.datapan\release-korad\reports\latest-verification.json --json
datapan catalog verify summary --input C:\workspace\datapan-registry\.datapan\release-korad\reports\latest-verification.json --output C:\workspace\datapan-registry\.datapan\release-korad\reports\latest-verification-summary.json --json
datapan catalog coverage --registry C:\workspace\datapan-registry\.datapan\release-korad\data\data-go-kr.registry.json --verification C:\workspace\datapan-registry\.datapan\release-korad\reports\latest-verification.json --output C:\workspace\datapan-registry\.datapan\release-korad\reports\coverage.json --json
datapan catalog verify plan --registry C:\workspace\datapan-registry\.datapan\release-korad\data\data-go-kr.registry.json --verification C:\workspace\datapan-registry\.datapan\release-korad\reports\latest-verification.json --output C:\workspace\datapan-registry\.datapan\release-korad\reports\verification-plan.json --json
```
