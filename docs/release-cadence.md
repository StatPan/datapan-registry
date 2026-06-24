# Release Cadence

`datapan-registry` releases should be boring, repeatable, and evidence-first.

## Inputs

- A current `datapan-cli` build.
- A data.go.kr API key available to the CLI environment.
- The previous released registry when available, usually extracted under
  `.datapan/previous/` from the last GitHub Release asset.

## Draft

Generate a release draft from the `datapan-registry` checkout:

```bash
datapan catalog update data-go-kr --registry data/data-go-kr.registry.json --apply --backup --diff-limit 0 --json
datapan catalog release draft --registry data/data-go-kr.registry.json --previous-registry .datapan/previous/data-go-kr.registry.json --verification reports/latest-verification.json --output-dir . --provider-limit 0 --json
```

When there is no previous release yet, omit `--previous-registry`.

## Verify

Every release must pass:

```bash
datapan catalog release verify --manifest manifest.json --output reports/latest-release-verification.json --json
datapan catalog release readiness --manifest manifest.json --output reports/latest-release-readiness.json --json
```

The repository also runs `.github/workflows/verify-release.yml` on pushes, pull
requests, manual dispatches, `v*` tags, and a weekly scheduled release-health
check. That workflow:

- checks out `datapan-registry` with Git LFS enabled;
- checks that `data/data-go-kr.registry.json` is the full materialized file,
  not an LFS pointer;
- checks out `StatPan/datapan-cli`;
- rereads provider-specific verification reports and regenerates bounded
  summaries for qnet, epost, ekape, forest, folk, airport, jeonju, geoje, and the
  merged latest report;
- runs `catalog release verify`;
- runs `catalog release readiness`;
- checks that the README Current Snapshot matches the generated coverage,
  provider-index, and verification-summary artifacts;
- installs the latest GitHub Release zip with
  `datapan catalog install datapan-registry`;
- runs `datapan doctor --json` against that installed registry.

Recommended evidence before tagging:

```bash
datapan catalog verify --registry data/data-go-kr.registry.json --limit 100 --output reports/latest-verification.json --json
datapan catalog verify summary --input reports/latest-verification.json --output reports/latest-verification-summary.json --json
```

Provider-specific evidence should be accumulated for registered external
adapters:

```bash
datapan catalog verify --registry data/data-go-kr.registry.json --provider q-net --kind external_endpoint --limit 5 --output reports/qnet-verification.json --json
datapan catalog verify summary --input reports/qnet-verification.json --output reports/qnet-verification-summary.json --json
datapan catalog verify --registry data/data-go-kr.registry.json --provider epost --kind external_endpoint --limit 5 --output reports/epost-verification.json --json
datapan catalog verify summary --input reports/epost-verification.json --output reports/epost-verification-summary.json --json
datapan catalog verify --registry data/data-go-kr.registry.json --provider ekape --kind external_endpoint --limit 5 --output reports/ekape-verification.json --json
datapan catalog verify summary --input reports/ekape-verification.json --output reports/ekape-verification-summary.json --json
datapan catalog verify --registry data/data-go-kr.registry.json --provider forest --kind external_endpoint --limit 4 --output reports/forest-verification.json --json
datapan catalog verify summary --input reports/forest-verification.json --output reports/forest-verification-summary.json --json
datapan catalog verify --registry data/data-go-kr.registry.json --provider folk --kind external_endpoint --limit 3 --output reports/folk-verification.json --json
datapan catalog verify summary --input reports/folk-verification.json --output reports/folk-verification-summary.json --json
datapan catalog verify --registry data/data-go-kr.registry.json --provider airport --kind external_endpoint --limit 6 --output reports/airport-verification.json --json
datapan catalog verify summary --input reports/airport-verification.json --output reports/airport-verification-summary.json --json
datapan catalog verify --registry data/data-go-kr.registry.json --provider jeonju --kind external_endpoint --limit 5 --output reports/jeonju-verification.json --json
datapan catalog verify summary --input reports/jeonju-verification.json --output reports/jeonju-verification-summary.json --json
datapan catalog verify --registry data/data-go-kr.registry.json --provider geoje --kind external_endpoint --limit 6 --output reports/geoje-verification.json --json
datapan catalog verify summary --input reports/geoje-verification.json --output reports/geoje-verification-summary.json --json
datapan catalog verify merge --input reports/qnet-verification.json --input reports/epost-verification.json --input reports/ekape-verification.json --input reports/forest-verification.json --input reports/folk-verification.json --input reports/airport-verification.json --input reports/jeonju-verification.json --input reports/geoje-verification.json --output reports/latest-verification.json --json
datapan catalog verify summary --input reports/latest-verification.json --output reports/latest-verification-summary.json --json
```

The merged report is the release evidence artifact. Provider-specific reports
may stay in `reports/` as supporting evidence, while
`reports/latest-verification.json` and
`reports/latest-verification-summary.json` are included in `manifest.json`.
Skipped and failed results are kept because they explain current provider
boundaries, such as WADL-only metadata, unsupported SOAP operations, separate
key registration requirements, or upstream provider HTML responses.

## Publish

1. Commit generated artifacts.
   Update README Current Snapshot from `reports/coverage.json`,
   `data/provider-index.json`, and `reports/latest-verification-summary.json`
   in the same commit.
2. Tag with `vYYYY.MM.DD`, or `vYYYY.MM.DD.N` for a second release on the same
   date.
3. Push the branch and tag.
4. Create a GitHub Release.
5. Attach a zip archive of the release snapshot so users can consume it without
   relying on Git LFS.
6. Confirm the `Verify registry release` workflow passes on the tag.

## Cadence

Start with date-based releases and keep a weekly scheduled health check running
between releases. The scheduled workflow does not publish a new release by
itself. It proves that the checked-in manifest remains verifiable and that the
latest public release asset remains installable.

Move from scheduled health checks to scheduled release drafting only after:

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
