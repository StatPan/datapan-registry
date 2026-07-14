# Healthcheck Registry release v2026.07.14

This release line publishes the reviewed public-data Healthcheck catalog from
Registry commit `b49d66b97d8155c34649f4dd2040b884c4212d64`. The published
source tag may contain release documentation in addition to that catalog
commit; consumers identify the catalog through the immutable artifact digests
below, not through a branch name.

## Consumption pin

- GitHub tag: `v2026.07.14`
- Hugging Face Dataset: `StatPan/datapan-registry`
- Dataset payload revision: `10f375182f992bc700468dd9d6e2930acd3bf8e8`
- Release manifest SHA-256:
  `0b78c286b8cfa889ddccf51f83a9d8adc4eac8617ea6d9fd2d66d1fcf668281f`
- Health-probe catalog SHA-256:
  `e84f0da2f532a32833def1118a4610bf2322f370783d120b84cf85306d244840`
- Canonical Registry SHA-256:
  `eeda72ee8590f458de8d75703662578e80edf3e61282f0e5e67547c4f6e5f644`

The Dataset pointer manifest and the GitHub snapshot asset both bind the
catalog and canonical Registry to those checksums. Healthcheck must store the
Dataset revision, release-manifest digest, catalog digest, and Registry digest
with every observation. It must never initialize from an unpinned branch
checkout or a direct catalog JSON URL without this provenance chain.

## Consumer boundary

Datapan CLI `v0.1.37` installs the release snapshot through its canonical
monolith fallback and verifies the Registry checksum. The ARM64 Healthcheck
scheduler validates the catalog checksum before it creates scheduler state.
The catalog contains ten reviewed canaries: five data.go.kr gateway routes and
five registered external-adapter routes.

Every entry is credential-required, has a request budget of one and a ten
second timeout ceiling. Credentials are supplied only at CLI execution time.
All entries use `not_asserted` freshness with an explicit rationale; Healthcheck
must report availability and last observation separately rather than deriving a
fresh/stale claim. Registry artifacts exclude credential values, query values,
response rows, mutable receipts, and live status.
