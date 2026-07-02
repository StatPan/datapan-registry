# data.go.kr Release Provenance

- generated_at: 2026-07-02T16:17:23Z
- datapan_version: 0.1.0-dev
- source_provider: data.go.kr
- source_registry: ..\datapan-registry\data\data-go-kr.registry.json
- previous_registry: ..\datapan-registry\.datapan\previous\data-go-kr.registry.json
- release_registry: ..\datapan-registry\data\data-go-kr.registry.json
- provider_limit: 0
- verification_source: ..\datapan-registry\reports\latest-verification.json
- unadapted_external_probe_source: ..\datapan-registry\reports\unadapted-external-probe.json

## Commands

```bash
datapan catalog release draft --registry ..\datapan-registry\data\data-go-kr.registry.json --output-dir ..\datapan-registry --provider-limit 0 --previous-registry ..\datapan-registry\.datapan\previous\data-go-kr.registry.json --verification ..\datapan-registry\reports\latest-verification.json --json
# provider index: ..\datapan-registry\data\provider-index.json
datapan catalog diff --old ..\datapan-registry\.datapan\previous\data-go-kr.registry.json --new ..\datapan-registry\data\data-go-kr.registry.json --limit 0 --output ..\datapan-registry\reports\catalog-diff.json --json
datapan catalog audit --registry ..\datapan-registry\data\data-go-kr.registry.json --output ..\datapan-registry\reports\catalog-audit.json --json
datapan catalog errors --registry ..\datapan-registry\data\data-go-kr.registry.json --output ..\datapan-registry\reports\error-catalog.json --json
datapan catalog dependencies --registry ..\datapan-registry\data\data-go-kr.registry.json --limit 0 --output ..\datapan-registry\reports\dependencies.json --json
datapan catalog adapter-targets --registry ..\datapan-registry\data\data-go-kr.registry.json --limit 0 --output ..\datapan-registry\reports\adapter-targets.json --json
datapan catalog route-disposition --registry ..\datapan-registry\data\data-go-kr.registry.json --probe ..\datapan-registry\reports\unadapted-external-probe.json --limit 0 --output ..\datapan-registry\reports\route-disposition.json --json
datapan catalog providers --registry ..\datapan-registry\data\data-go-kr.registry.json --limit 0 --output ..\datapan-registry\reports\provider-backlog.json --json
datapan catalog verify --input ..\datapan-registry\reports\latest-verification.json --json
datapan catalog verify summary --input ..\datapan-registry\reports\latest-verification.json --output ..\datapan-registry\reports\latest-verification-summary.json --json
datapan catalog verify --input ..\datapan-registry\reports\unadapted-external-probe.json --json
datapan catalog verify summary --input ..\datapan-registry\reports\unadapted-external-probe.json --output ..\datapan-registry\reports\unadapted-external-probe-summary.json --json
datapan catalog coverage --registry ..\datapan-registry\data\data-go-kr.registry.json --verification ..\datapan-registry\reports\latest-verification.json --route-disposition ..\datapan-registry\reports\route-disposition.json --output ..\datapan-registry\reports\coverage.json --json
datapan catalog verify plan --registry ..\datapan-registry\data\data-go-kr.registry.json --verification ..\datapan-registry\reports\latest-verification.json --output ..\datapan-registry\reports\verification-plan.json --json
```
