# Diagnostic contract publication handoff

This change prepares release-bound artifacts; it does not publish them. The
accepted candidate is pinned by binding
`ac847cb158eb432e72e78d194a94542db5860062b9b869c42f6d736e4f649016`,
candidate head `5343ebc4477640409d76cac5bee71e0824e48d59`, and merge commit
`114c4e4a043bc495ada04e5c85fe8bed4eaf1fc3`.

Before publication, require exact-head independent approval and hosted CI for
this implementation PR. An operator may then make a separate decision to tag,
create a GitHub Release, and manually dispatch the Hugging Face workflow with
`publish=true`. A push to `main` only validates and stages; it cannot publish.

After publication, clear all credentials and verify the public pointer with the
full immutable payload revision. Add one `--require-artifact PATH=SHA256`
argument for `manifest.json` and for every artifact listed in
`reports/diagnostic-publication-readiness.json`:

```sh
HF_TOKEN='' python scripts/huggingface_registry_distribution.py verify-remote \
  --pointer-url https://huggingface.co/datasets/StatPan/datapan-registry/resolve/main/release/distribution-manifest.json \
  --expected-revision FULL_NONZERO_PAYLOAD_COMMIT \
  --require-artifact manifest.json=EXACT_RELEASE_MANIFEST_SHA256 \
  --require-artifact schemas/datapan.diagnostic-envelope.v1.schema.json=EXACT_SCHEMA_SHA256
```

The verifier rejects mutable, zero, malformed, or unexpected payload revisions,
missing paths, and byte/SHA-256 drift. Only a successful anonymous proof may
unblock a Datapan Web adoption issue. That issue must pin the immutable payload
revision and all diagnostic artifact identities. Web owns presentation and
mutable history; Registry owns stable facts, vocabulary, and immutable artifact
identity. No provider response body, credentials, secret hashes, authorization
headers, live status history, or user telemetry belongs in either handoff.

The workflow's automatic post-publish receipt is only the baseline distribution
integrity proof: it pins the payload revision returned by that publication and
verifies every artifact declared by the downloaded pointer. It does not pass the
checked-in `--require-artifact PATH=SHA256` set, so it does not by itself satisfy
or replace the Datapan Web adoption gate. A later credential-free operator proof
must require `manifest.json` plus every exact artifact identity in
`reports/diagnostic-publication-readiness.json` before Web adoption is unblocked.
