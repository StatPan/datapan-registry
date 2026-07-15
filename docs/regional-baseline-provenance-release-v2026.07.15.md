# Regional baseline provenance Registry release v2026.07.15

This is the preparation record for a planned signed Registry release containing
the reviewed KOSIS provenance contract for `korea.regional_baseline.v0`. It does
not create the tag, GitHub Release, release assets, or another Hugging Face
publication.

## Source ancestry and immutable pins

The eventual release source commit must contain merged Registry PR #560 commit
`9730e7ed027ec6b291141ebf69912a4e0452e5e9` in its ancestry. That commit added
the manifest-bound provenance schema and artifact without changing the
canonical Registry, an existing consumer contract, or credential/runtime
policy.

The reviewed bytes at that source commit are:

- planned GitHub tag: `v2026.07.15`;
- release manifest: `manifest.json`;
- release manifest SHA-256:
  `b1b1d18c958fc2fffb5396ade2d1ef13e67c77e5645a289ccdbb11e73e552999`;
- provenance artifact:
  `reports/regional-baseline-source-provenance.json`;
- provenance artifact SHA-256:
  `e0436c05d0eaba44bfca1f31b11f4a6bd69273dc76ff72a381863c2184cc9553`;
- provenance schema:
  `https://schemas.datapan.dev/datapan.source-provenance.v1.schema.json`.

The tagged manifest must bind exactly one artifact at the canonical provenance
path with `kind=source_provenance`, that schema URI, and the exact artifact byte
count and SHA-256. A tag must not be published if its tagged blobs produce
different hashes.

## Existing Hugging Face distribution

The merge of #560 already triggered the repository's two-phase Hugging Face
publication workflow. It published and anonymously verified:

- Dataset: `StatPan/datapan-registry`;
- payload revision: `5a5a55fb10318f9951b6dee4b4a798aea2c6fd4a`;
- pointer revision: `775043bdb886e5a9389a04c890bb93eea49a0dab`;
- source workflow run:
  `https://github.com/StatPan/datapan-registry/actions/runs/29421487991`.

Release preparation re-verifies the public pointer anonymously. It must not run
the publish mode again or mutate the Hugging Face `main` pointer. The signed tag
and GitHub Release are additional immutable compatibility surfaces for the
already published manifest-bound bytes.

## datapan-data consumer verification

datapan-data must derive, rather than invent, the Registry consumer pin:

1. resolve `v2026.07.15` in the canonical Registry checkout;
2. read `manifest.json` and the canonical provenance artifact from that tag;
3. verify the tagged manifest bytes against the manifest SHA-256 above;
4. require exactly one canonical manifest entry with the expected kind, schema,
   byte count, and provenance SHA-256;
5. verify the tagged provenance bytes against that entry; and
6. derive `registry_tag`, `registry_manifest_sha256`, and
   `provenance_artifact_sha256` from those verified bytes.

A missing tag, partial Registry input set, worktree/tag byte mismatch, duplicate
or drifted manifest entry, or candidate-supplied placeholder pin must fail
closed. Registry owns source identity and conditional rights provenance only.
datapan-data continues to own capture evidence, immutable data releases,
freshness review, and `current.json` selection.

## Public-data boundary

The provenance artifact describes the exact KOSIS eRegion endpoint family,
three reviewed indicators and tables, conditional domestic-statistics reuse,
the unchanged-raw-paid-redistribution prohibition, and explicit revalidation
triggers. Freshness remains `not_asserted` in Registry. The release does not
contain credential values, raw response rows or snapshots, mutable Healthcheck
observations, CLI runtime state, dataset delivery locators, or a claim that the
2024 snapshot is current.

## Scoped release-readiness decision

Global `gira release readiness` currently reports unrelated draft PR #524 as a
missing-approval and policy-violation blocker. The release owner excludes #524
from the scope of this tag because it concerns transactional freshness imports,
does not change #560 or this provenance trust chain, and was already open when
release issue #557 successfully published `v2026.07.14`.

This scoped decision does not approve, merge, close, relabel, or otherwise
change #524. If repository policy requires global readiness to be true despite
this recorded owner rationale, publication stops and returns to root review.

## Publication gate

Before any external publication, root must approve the exact preparation commit
and release assets. The operator must then:

1. verify the release ledger fixed point and provenance generator/validator;
2. run the guarded release-draft workflow in `verify-only` mode and inspect the
   packaged snapshot and optional shard archive;
3. create and push an annotated signed tag only at the approved commit;
4. verify the tag signature, tagged blob hashes, and tag-triggered Registry CI;
5. publish the GitHub Release and checked assets; and
6. record anonymous installation and datapan-data pin-verification evidence.

Until that separate approval, signed tag push, GitHub Release or asset
publication, Hugging Face publication/current mutation, merge of this
preparation PR, and Gira finish are prohibited.
