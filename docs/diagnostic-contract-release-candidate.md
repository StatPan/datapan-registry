# Diagnostic contract release candidate

The diagnostic envelope remains a draft until all three required consumers provide
exact-head compatibility proof and an independent publication review accepts the
assembled result. The checked-in release candidate is evidence for that decision;
it is not a public release manifest and has no runtime authority.

`consumer-proof-intake.v1.json` records the exact repository, pull request, head
commit, CI, and review state observed for each consumer. The generator binds that
intake to the exact bytes of the schema, consumer contract, evidence mapping, and
three compatibility packets. The validator rejects stale output, missing blockers,
any authority flag set to true, or accidental inclusion of diagnostic draft paths
in the public manifest or schema index.

Current interpretation:

- `datapan-health` has accepted offline contract compatibility proof.
- `datapan-web` has an approved dependency-independent slice, but still lacks the
  immutable Registry manifest and public Health identity composition.
- `datapan-cli` remains blocked while Registry Journey CI and independent exact-head
  approval are missing.

Regenerate after an exact consumer proof changes:

```sh
python scripts/generate-diagnostic-release-candidate.py
python scripts/validate-diagnostic-release-candidate.py
python -m unittest tests/test_diagnostic_release_candidate.py
```

Even when every consumer becomes accepted, the generator only changes the state to
`ready_for_publication_review`; it never grants publishing authority. Public schema
indexing, root manifest changes, tagging, and distribution require a separate,
independently reviewed release change.
