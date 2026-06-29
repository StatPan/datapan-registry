# data.go.kr Release Provenance

- generated_at: 2026-06-29T09:17:25Z
- datapan_version: 0.1.0-dev
- source_provider: data.go.kr
- source_registry: /home/statpan/workspace/opensource/datapan-registry/data/data-go-kr.registry.json
- previous_registry: /home/statpan/workspace/opensource/datapan-registry/.datapan/previous/data-go-kr.registry.json
- release_registry: /home/statpan/workspace/opensource/datapan-registry/data/data-go-kr.registry.json
- provider_limit: 0
- verification_source: /home/statpan/workspace/opensource/datapan-registry/reports/latest-verification.json
- unadapted_external_probe_source: /home/statpan/workspace/opensource/datapan-registry/reports/unadapted-external-probe.json

## Commands

```bash
datapan catalog release draft --registry /home/statpan/workspace/opensource/datapan-registry/data/data-go-kr.registry.json --output-dir /home/statpan/workspace/opensource/datapan-registry --provider-limit 0 --previous-registry /home/statpan/workspace/opensource/datapan-registry/.datapan/previous/data-go-kr.registry.json --verification /home/statpan/workspace/opensource/datapan-registry/reports/latest-verification.json --json
# provider index: /home/statpan/workspace/opensource/datapan-registry/data/provider-index.json
datapan catalog diff --old /home/statpan/workspace/opensource/datapan-registry/.datapan/previous/data-go-kr.registry.json --new /home/statpan/workspace/opensource/datapan-registry/data/data-go-kr.registry.json --limit 0 --output /home/statpan/workspace/opensource/datapan-registry/reports/catalog-diff.json --json
datapan catalog audit --registry /home/statpan/workspace/opensource/datapan-registry/data/data-go-kr.registry.json --output /home/statpan/workspace/opensource/datapan-registry/reports/catalog-audit.json --json
datapan catalog errors --registry /home/statpan/workspace/opensource/datapan-registry/data/data-go-kr.registry.json --output /home/statpan/workspace/opensource/datapan-registry/reports/error-catalog.json --json
datapan catalog dependencies --registry /home/statpan/workspace/opensource/datapan-registry/data/data-go-kr.registry.json --limit 0 --output /home/statpan/workspace/opensource/datapan-registry/reports/dependencies.json --json
datapan catalog adapter-targets --registry /home/statpan/workspace/opensource/datapan-registry/data/data-go-kr.registry.json --limit 0 --output /home/statpan/workspace/opensource/datapan-registry/reports/adapter-targets.json --json
datapan catalog route-disposition --registry /home/statpan/workspace/opensource/datapan-registry/data/data-go-kr.registry.json --probe /home/statpan/workspace/opensource/datapan-registry/reports/unadapted-external-probe.json --limit 0 --output /home/statpan/workspace/opensource/datapan-registry/reports/route-disposition.json --json
datapan catalog providers --registry /home/statpan/workspace/opensource/datapan-registry/data/data-go-kr.registry.json --limit 0 --output /home/statpan/workspace/opensource/datapan-registry/reports/provider-backlog.json --json
datapan catalog verify --input /home/statpan/workspace/opensource/datapan-registry/reports/latest-verification.json --json
datapan catalog verify summary --input /home/statpan/workspace/opensource/datapan-registry/reports/latest-verification.json --output /home/statpan/workspace/opensource/datapan-registry/reports/latest-verification-summary.json --json
datapan catalog verify --input /home/statpan/workspace/opensource/datapan-registry/reports/unadapted-external-probe.json --json
datapan catalog verify summary --input /home/statpan/workspace/opensource/datapan-registry/reports/unadapted-external-probe.json --output /home/statpan/workspace/opensource/datapan-registry/reports/unadapted-external-probe-summary.json --json
datapan catalog coverage --registry /home/statpan/workspace/opensource/datapan-registry/data/data-go-kr.registry.json --verification /home/statpan/workspace/opensource/datapan-registry/reports/latest-verification.json --route-disposition /home/statpan/workspace/opensource/datapan-registry/reports/route-disposition.json --output /home/statpan/workspace/opensource/datapan-registry/reports/coverage.json --json
datapan catalog verify plan --registry /home/statpan/workspace/opensource/datapan-registry/data/data-go-kr.registry.json --verification /home/statpan/workspace/opensource/datapan-registry/reports/latest-verification.json --output /home/statpan/workspace/opensource/datapan-registry/reports/verification-plan.json --json
```
