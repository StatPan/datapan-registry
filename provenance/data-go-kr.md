# data.go.kr Release Provenance

- generated_at: 2026-06-29T14:15:08Z
- datapan_version: 0.1.0-dev
- source_provider: data.go.kr
- source_registry: data\data-go-kr.registry.json
- previous_registry: data\data-go-kr.registry.json
- release_registry: .\data\data-go-kr.registry.json
- provider_limit: 0
- verification_source: reports\latest-verification.json
- unadapted_external_probe_source: .\reports\unadapted-external-probe.json

## Commands

```bash
datapan catalog release draft --registry data\data-go-kr.registry.json --output-dir . --provider-limit 0 --previous-registry data\data-go-kr.registry.json --verification reports\latest-verification.json --json
# provider index: .\data\provider-index.json
datapan catalog diff --old data\data-go-kr.registry.json --new .\data\data-go-kr.registry.json --limit 0 --output .\reports\catalog-diff.json --json
datapan catalog audit --registry .\data\data-go-kr.registry.json --output .\reports\catalog-audit.json --json
datapan catalog errors --registry .\data\data-go-kr.registry.json --output .\reports\error-catalog.json --json
datapan catalog dependencies --registry .\data\data-go-kr.registry.json --limit 0 --output .\reports\dependencies.json --json
datapan catalog adapter-targets --registry .\data\data-go-kr.registry.json --limit 0 --output .\reports\adapter-targets.json --json
datapan catalog route-disposition --registry .\data\data-go-kr.registry.json --probe .\reports\unadapted-external-probe.json --limit 0 --output .\reports\route-disposition.json --json
datapan catalog providers --registry .\data\data-go-kr.registry.json --limit 0 --output .\reports\provider-backlog.json --json
datapan catalog verify --input .\reports\latest-verification.json --json
datapan catalog verify summary --input .\reports\latest-verification.json --output .\reports\latest-verification-summary.json --json
datapan catalog verify --input .\reports\unadapted-external-probe.json --json
datapan catalog verify summary --input .\reports\unadapted-external-probe.json --output .\reports\unadapted-external-probe-summary.json --json
datapan catalog coverage --registry .\data\data-go-kr.registry.json --verification .\reports\latest-verification.json --route-disposition .\reports\route-disposition.json --output .\reports\coverage.json --json
datapan catalog verify plan --registry .\data\data-go-kr.registry.json --verification .\reports\latest-verification.json --output .\reports\verification-plan.json --json
```
