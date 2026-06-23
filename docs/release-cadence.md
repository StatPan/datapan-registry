# Release Cadence

`datapan-registry` releases should be boring, repeatable, and evidence-first.

## Inputs

- A current `datapan-cli` build.
- A data.go.kr API key available to the CLI environment.
- The previous released registry when available.

## Draft

Generate a release draft from `datapan-cli`:

```bash
datapan catalog update data-go-kr --registry data/data-go-kr.registry.json --apply --backup --diff-limit 0 --json
datapan catalog release draft --registry data/data-go-kr.registry.json --previous-registry previous/data-go-kr.registry.json --verification reports/latest-verification.json --output-dir .datapan/release --provider-limit 0 --json
```

When there is no previous release yet, omit `--previous-registry`.

## Verify

Every release must pass:

```bash
datapan catalog release verify --manifest manifest.json --output reports/latest-release-verification.json --json
datapan catalog release readiness --manifest manifest.json --output reports/latest-release-readiness.json --json
```

Recommended evidence before tagging:

```bash
datapan catalog verify --registry data/data-go-kr.registry.json --limit 100 --output reports/latest-verification.json --json
datapan catalog verify summary --input reports/latest-verification.json --output reports/latest-verification-summary.json --json
```

Provider-specific evidence should be accumulated for registered external
adapters:

```bash
datapan catalog verify --registry data/data-go-kr.registry.json --provider q-net --kind external_endpoint --limit 5 --output reports/qnet-verification.json --json
datapan catalog verify --registry data/data-go-kr.registry.json --provider epost --kind external_endpoint --limit 5 --output reports/epost-verification.json --json
```

## Publish

1. Commit generated artifacts.
2. Tag with `vYYYY.MM.DD`.
3. Push the branch and tag.
4. Create a GitHub Release.
5. Attach a zip archive of the release snapshot so users can consume it without
   relying on Git LFS.

## Cadence

Start with manual date-based releases. Move to scheduled automation only after:

- the import command is stable across repeated full catalog pulls;
- release verification and readiness reports are consistently useful;
- provider adapter evidence is improving across releases;
- consumers can pin either a git tag or a release asset.

## Non-Goals

- Do not store credentials.
- Do not edit generated JSON artifacts by hand.
- Do not claim every API is callable merely because it appears in the catalog.
- Do not remove failed or skipped verification evidence just to make the release
  look cleaner.
