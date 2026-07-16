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
  and non-public runtime boundaries are revalidated locally.
- `datapan-web` at `79da6545df44b8d3933e482bb86895028522de73` has 55 passing
  local tests, a passing production build and audit, and completed Health identity
  composition (AC3, 7/8 overall). The invalid-clock freshness defect is remediated.
  It remains partial because immutable Registry release consumption and final
  independent exact-head approval are absent.
- `datapan-cli` remains blocked. Standard CI and local Go gates pass, but Registry
  Journey is externally unavailable and independent review found that product
  metrics and the executable failure-to-success export journey are not connected
  to production CLI output.

Regenerate after an exact consumer proof changes:

```sh
python scripts/generate-diagnostic-release-candidate.py
python scripts/validate-diagnostic-release-candidate.py
python -m unittest tests/test_diagnostic_release_candidate.py
```

After every consumer has a machine proof and consumer-specific semantic validator,
the generator may change the state to `ready_for_publication_review`; it still never
grants publishing authority. Public schema indexing, root manifest changes, tagging,
and distribution require a separate, independently reviewed release change.
