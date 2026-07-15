# Regional baseline provenance release v2026.07.15 execution receipt

This receipt records the public execution of the release prepared in
`docs/regional-baseline-provenance-release-v2026.07.15.md`. It contains no
credential, token, private key material, raw KOSIS rows, or mutable consumer
state.

## Approval boundary

Root approved both irreversible gates before the first public mutation:

1. publish the SSH-signed annotated tag `v2026.07.15` at exact commit
   `33eed374a5ae70eb92b738ce587309814e791576` and the reviewed GitHub Release
   assets; and
2. exclude unrelated draft PR #524 only from the global Gira readiness scope of
   this tag without changing or approving #524.

The approval and scoped exception are recorded in
[Registry issue #563](https://github.com/StatPan/datapan-registry/issues/563#issuecomment-4986038115).
The manifest-level release readiness result did not suppress or redefine the
global Gira blocker.

## Pre-publication fixed point

The final preflight receipt is recorded in
[issue #563](https://github.com/StatPan/datapan-registry/issues/563#issuecomment-4986047415).
Immediately before tag creation it proved:

- target commit:
  `33eed374a5ae70eb92b738ce587309814e791576`;
- target tree: `0c8e148249250ee235c53cd39b57875b35bdb740`;
- provenance ancestor:
  `9730e7ed027ec6b291141ebf69912a4e0452e5e9`;
- release manifest SHA-256:
  `b1b1d18c958fc2fffb5396ade2d1ef13e67c77e5645a289ccdbb11e73e552999`;
- canonical provenance SHA-256:
  `e0436c05d0eaba44bfca1f31b11f4a6bd69273dc76ff72a381863c2184cc9553`;
- provenance byte count: `3049`; and
- exactly one canonical `source_provenance` manifest entry with the reviewed
  schema, byte count, and digest.

The guarded verify-only workflow
[run 29423537728](https://github.com/StatPan/datapan-registry/actions/runs/29423537728)
was generated from tree `0c8e1482...740`, identical to the tag target tree.
Its fresh-downloaded snapshot, shard archive, package check, and cross-asset
check all passed before publication.

## Signed tag and CI

- public tag: [`v2026.07.15`](https://github.com/StatPan/datapan-registry/releases/tag/v2026.07.15);
- annotated tag object:
  `a30614b64db7bbc53e0c3bc1f3a8341d897597a4`;
- peeled commit:
  `33eed374a5ae70eb92b738ce587309814e791576`;
- signature: GitHub `verified=true`, `reason=valid`, using the registered
  `Datapan Registry release signing` SSH key; and
- tag-triggered verification:
  [run 29456899644](https://github.com/StatPan/datapan-registry/actions/runs/29456899644),
  completed successfully.

The tag workflow passed release-policy validation, provider evidence,
manifest verification, release readiness, full shard release evidence,
packaging, installation, and doctor checks. The separate
`Registry shard validation` workflow is configured for `main` pushes rather
than tag pushes; the exact target commit passed that workflow in
[run 29423794826](https://github.com/StatPan/datapan-registry/actions/runs/29423794826),
while the tag workflow independently repeated the full release shard check.

## GitHub Release assets

The non-draft, non-prerelease
[GitHub Release](https://github.com/StatPan/datapan-registry/releases/tag/v2026.07.15)
was created with `--verify-tag`, preventing creation of a substitute tag.

| Asset | Bytes | SHA-256 |
| --- | ---: | --- |
| `datapan-registry-v2026.07.15.zip` | 11,791,879 | `60471daed75ffd1722090c3c386a689f03b2d07721a8dcdad075bb8852e46837` |
| `data-go-kr-shards.tar.gz` | 7,989,462 | `db6fed64651a59bdb3c77dd2a1eec28c739deb64d35f78567629138b897a25aa` |

GitHub reports the same digests. Anonymous downloads with `GH_TOKEN`,
`GITHUB_TOKEN`, `HF_TOKEN`, and data-provider credential variables removed
reproduced both byte counts and digests. The release package check found 141
entries; the shard check found 411 shards and 12,060 records; the cross-asset
check matched the canonical registry digest
`eeda72ee8590f458de8d75703662578e80edf3e61282f0e5e67547c4f6e5f644`.

## Anonymous consumer verification

The public Release API URL was installed in an empty temporary directory with a
fresh current `datapan-cli` build and all GitHub, Hugging Face, and provider
credential variables removed:

```text
datapan catalog install datapan-registry \
  --release-url https://api.github.com/repos/StatPan/datapan-registry/releases/tags/v2026.07.15 \
  --json
datapan doctor --json
```

The install returned `release_tag=v2026.07.15`, `pin_mode=pinned`, 12,060
specs, a verified manifest-bound registry, 411 validated shards containing
12,060 records, and `safe_to_consume`. Doctor exited successfully with the
active registry and digest matching, `registry_trust.status=trusted`,
`integrity=verified`, `manifest_binding=verified`, and
`execution_allowed=true`. Provider credentials were absent, proving the public
installation boundary without claiming provider-call readiness.

## Unchanged Hugging Face distribution

Anonymous post-publication resolution confirmed that Hugging Face `main` still
resolves through pointer revision
`775043bdb886e5a9389a04c890bb93eea49a0dab` to payload revision
`5a5a55fb10318f9951b6dee4b4a798aea2c6fd4a`. Its distribution manifest retains
the release manifest digest above. No Hugging Face publish workflow or pointer
mutation occurred during this execution; the latest publish run remains
[29421487991](https://github.com/StatPan/datapan-registry/actions/runs/29421487991).

## Consumer handoff and correction policy

The canonical consumer pin is:

```text
registry_tag=v2026.07.15
registry_manifest_sha256=b1b1d18c958fc2fffb5396ade2d1ef13e67c77e5645a289ccdbb11e73e552999
provenance_artifact_sha256=e0436c05d0eaba44bfca1f31b11f4a6bd69273dc76ff72a381863c2184cc9553
```

The public trust-chain evidence and instruction to derive rather than copy the
pin were handed to
[datapan-data #1090](https://github.com/StatPan/datapan-data/issues/1090#issuecomment-4986092181).

The tag and published asset bytes are immutable compatibility surfaces. They
must not be force-moved, deleted, or replaced. A discovered error is corrected
only through a new signed tag and release.
