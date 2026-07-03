# data.go.kr Release Provenance

- generated_at: 2026-07-03T13:07:56Z
- datapan_version: 0.1.0-dev
- source_provider: data.go.kr
- source_registry: C:\Users\statp\dev\datapan-registry\data\data-go-kr.registry.json
- previous_registry: C:\Users\statp\dev\datapan-registry\.datapan\previous\data-go-kr.registry.json
- release_registry: C:\Users\statp\dev\datapan-registry\data\data-go-kr.registry.json
- provider_limit: 0
- verification_source: C:\Users\statp\dev\datapan-registry\reports\latest-verification.json
- unadapted_external_probe_source: C:\Users\statp\dev\datapan-registry\reports\unadapted-external-probe.json

## Commands

```bash
datapan catalog release draft --registry C:\Users\statp\dev\datapan-registry\data\data-go-kr.registry.json --output-dir C:\Users\statp\dev\datapan-registry --provider-limit 0 --previous-registry C:\Users\statp\dev\datapan-registry\.datapan\previous\data-go-kr.registry.json --verification C:\Users\statp\dev\datapan-registry\reports\latest-verification.json --json
# provider index: C:\Users\statp\dev\datapan-registry\data\provider-index.json
datapan catalog diff --old C:\Users\statp\dev\datapan-registry\.datapan\previous\data-go-kr.registry.json --new C:\Users\statp\dev\datapan-registry\data\data-go-kr.registry.json --limit 0 --output C:\Users\statp\dev\datapan-registry\reports\catalog-diff.json --json
datapan catalog audit --registry C:\Users\statp\dev\datapan-registry\data\data-go-kr.registry.json --output C:\Users\statp\dev\datapan-registry\reports\catalog-audit.json --json
datapan catalog errors --registry C:\Users\statp\dev\datapan-registry\data\data-go-kr.registry.json --output C:\Users\statp\dev\datapan-registry\reports\error-catalog.json --json
datapan catalog dependencies --registry C:\Users\statp\dev\datapan-registry\data\data-go-kr.registry.json --limit 0 --output C:\Users\statp\dev\datapan-registry\reports\dependencies.json --json
datapan catalog adapter-targets --registry C:\Users\statp\dev\datapan-registry\data\data-go-kr.registry.json --limit 0 --output C:\Users\statp\dev\datapan-registry\reports\adapter-targets.json --json
datapan catalog route-disposition --registry C:\Users\statp\dev\datapan-registry\data\data-go-kr.registry.json --probe C:\Users\statp\dev\datapan-registry\reports\unadapted-external-probe.json --limit 0 --output C:\Users\statp\dev\datapan-registry\reports\route-disposition.json --json
datapan catalog providers --registry C:\Users\statp\dev\datapan-registry\data\data-go-kr.registry.json --limit 0 --output C:\Users\statp\dev\datapan-registry\reports\provider-backlog.json --json
datapan catalog verify --input C:\Users\statp\dev\datapan-registry\reports\latest-verification.json --json
datapan catalog verify summary --input C:\Users\statp\dev\datapan-registry\reports\latest-verification.json --output C:\Users\statp\dev\datapan-registry\reports\latest-verification-summary.json --json
datapan catalog verify --input C:\Users\statp\dev\datapan-registry\reports\unadapted-external-probe.json --json
datapan catalog verify summary --input C:\Users\statp\dev\datapan-registry\reports\unadapted-external-probe.json --output C:\Users\statp\dev\datapan-registry\reports\unadapted-external-probe-summary.json --json
datapan catalog coverage --registry C:\Users\statp\dev\datapan-registry\data\data-go-kr.registry.json --verification C:\Users\statp\dev\datapan-registry\reports\latest-verification.json --route-disposition C:\Users\statp\dev\datapan-registry\reports\route-disposition.json --output C:\Users\statp\dev\datapan-registry\reports\coverage.json --json
datapan catalog verify plan --registry C:\Users\statp\dev\datapan-registry\data\data-go-kr.registry.json --verification C:\Users\statp\dev\datapan-registry\reports\latest-verification.json --output C:\Users\statp\dev\datapan-registry\reports\verification-plan.json --json
```
