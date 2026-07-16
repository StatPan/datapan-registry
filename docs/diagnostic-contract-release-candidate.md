# Diagnostic contract release candidate

The diagnostic envelope remains a draft until all three required consumers provide
exact-head compatibility proof and an independent publication review accepts the
assembled result. The checked-in release candidate is evidence for that decision;
it is not a public release manifest and has no runtime authority.

`consumer-proof-intake.v1.json` records the exact repository, pull request, head
commit, CI, and review state observed for each consumer. The generator binds that
intake to the exact bytes of the schema, consumer contract, evidence mapping, and
three compatibility packets. An accepted consumer must additionally reference a
checked-in machine proof by path, byte length, SHA-256, and schema version. The
generator validates consumer-specific semantics instead of trusting free-form CI
or review labels. It also requires exactly three unique consumer records. Consumers
without an implemented semantic proof validator cannot become accepted.

The validator rejects stale output, missing blockers, proof byte or semantic drift,
duplicate consumers, any authority flag set to true, or accidental inclusion of
diagnostic draft paths in the public manifest or schema index.

Current interpretation:

- `datapan-health` has an accepted 7,968-byte offline compatibility receipt. Its
  exact contracts, 11 fixtures, 10 one-to-one operation bindings, 12-test proof,
  and non-public runtime boundaries are revalidated locally. The Registry
  validator independently pins the accepted Health head, tested revision, CI
  run, exact ten operation/dataset/service tuples and tuple digest, plus the
  exact test names, source digests, and test-manifest digest. Rewriting both the
  receipt and intake cannot establish a different proof identity.
- `datapan-web` at `79da6545df44b8d3933e482bb86895028522de73` has 55 passing
  local tests, a passing production build and zero-vulnerability audit, completed
  Health identity composition, and an independent exact-head approval. Its
  checked-in pre-publication receipt pins the exact schema and consumer-contract
  bytes, all 11 fixtures, 11-code cause/action/redaction behavior, seven journey
  contracts, diagnostic sources and package identities. This is sufficient for
  consumer compatibility before publication and does not claim a public release.
  Immutable Registry manifest consumption is intentionally a post-publication
  rollout gate; requiring it here would make publication depend on an artifact
  that publication itself creates.
- `datapan-cli` at `1800bef05c62c918b34a430d8d703ce2ed1afc1f` has an accepted
  pre-publication receipt and independent exact-head approval. The receipt pins
  the schema, mapping, all 11 fixtures, diagnostic implementation and handoff
  sources, seven real `Run` journeys, runtime-owned diagnosis and first-success
  metrics, JSON reuse and the actual `writeCSV` byte boundary. Local test, vet,
  both command builds and diff checks pass; ordinary CI run `29483717200` is
  green on Ubuntu, macOS and Windows.
- Anonymous Registry distribution remains a separate blocked publication gate.
  Registry Journey run `29483717293` built the exact CLI on all three operating
  systems but timed out awaiting Hugging Face response headers during bounded
  `init`. This external result does not revoke CLI compatibility and also cannot
  be relabeled as a passing public distribution proof.

Regenerate after an exact consumer proof changes:

```sh
python scripts/generate-diagnostic-release-candidate.py
python scripts/validate-diagnostic-release-candidate.py
python -m unittest tests/test_diagnostic_release_candidate.py
```

Every consumer now has a machine proof and consumer-specific semantic validator,
but the candidate remains `blocked` while anonymous Registry distribution is
unavailable. Only all consumer proofs plus all publication gates may change the
state to `ready_for_publication_review`; even then the generator never grants
publishing authority. Public schema indexing, root manifest changes, tagging, and
distribution require a separate, independently reviewed release change. Web's
immutable public manifest fetch is verified only after that release exists and is
not allowed to retroactively grant authority to this candidate.
