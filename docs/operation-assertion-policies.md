# Operation assertion policies

`drafts/operation-assertion-policies/operation-assertion-policies.v1.json` is
the Registry-owned, immutable release-candidate
expectation set for the ten Health canaries. It is not a live health receipt and
does not authorize a probe, alert, deployment, or publication.

Each operation has five explicit dimensions: transport, contract, presence,
semantic, and freshness. An assertion is allowed only when its `evidence`
selects one immutable Registry operation revision and records an allowlisted
rationale. A dimension without reviewed evidence is present as
`not_asserted`; consumers project that state to `not_observed`, never to pass or
fail. A missing dimension is malformed policy and Registry validation rejects
it.

Version 1 asserts only the normalized response-field vocabulary declared by
Registry. Transport expectations, minimum record presence, domain semantics,
freshness, and empty-result health have not been reviewed, so all ten canaries
retain explicit `not_asserted` entries for those dimensions. HTTP success or a
single current response is not evidence for adding them.

## Immutable evolution

Published policy history is append-only. A changed operation rule creates a
new artifact and increments `policy_set.version`; it does not rewrite an older
artifact. Version 1 has `supersedes_sha256: null`. Every later version must put
the previous artifact's canonical SHA-256 in `supersedes_sha256`, retain a new
canonical digest of its own, and receive a new manifest and consumer-proof
binding. Consumers that do not support the exact version, artifact digest,
Registry revision, or diagnostic vocabulary digest return `unknown`.

Freshness assertions, when domain evidence exists, must bind the response
reference-time field and format, Health observation-time source, timezone,
calendar, inclusive maximum-age boundary, future tolerance, empty-result
policy, and complete evaluated-state vocabulary. Until all of those are
reviewed, freshness remains `not_asserted`.

The private release manifest and candidate under
`drafts/operation-assertion-policies/` bind the schema, artifact, and offline
Datapan Health proof without adding them to the public Registry manifest. Its release,
runtime, and publishing authorities are all false. Hugging Face publication
and Datapan Health rollout remain separate decisions.
