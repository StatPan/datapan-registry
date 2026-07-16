# Diagnostic Envelope v1 Draft

`datapan.diagnostic-envelope.v1` is the proposed minimum contract shared by
Datapan CLI, Healthcheck, and Web when they explain whether a public-data
operation is usable and, if not, what the user should do next.

This is a review draft. Its schema lives under
`drafts/diagnostic-envelope/`, not the public `schemas/` directory. It is not
listed in `schemas/index.json`, bound by `manifest.json`, staged for public
distribution, or authorized as runtime truth. Publication is a separate goal
step after all three consumers provide compatibility evidence.

## Boundary

Registry owns the stable vocabulary, required evidence shape, responsibility
categories, safe action identifiers, avoidance identifiers, redaction rules,
and deterministic examples. A consumer owns the actual observation, inference,
mutable receipt or history, credential access, telemetry, and UI copy.

An envelope never contains a credential or secret-derived hash, authorization
header, credential-bearing request URL, raw provider text or URL, request or
response body, response rows, or user identity. `evidence_refs` carry bounded
identifiers and typed metadata instead of raw evidence.

## Minimum envelope

Every instance identifies when the assessment was made, the source and bounded
subject, one cause and determination, the accountable party, recommended and
avoided actions, typed evidence references, and a fail-closed redaction
attestation.

There is one certainty axis:

- `observed`: the named authority directly supplied or validated the fact;
- `inferred`: multiple bounded facts support the conclusion, but no authority
  states that conclusion directly;
- `unknown`: available evidence does not justify a more specific cause.

There is no numeric or probability-like confidence field. Consumers must not
turn `inferred` into a probability.

`assessed_at` records the conclusion time. Every evidence reference also names
its authority, observation time, exact scope, and a safe version identifier
when the conclusion depends on a policy or contract. Checked-in examples use
fictional fixture identities and declare `fixture.status=deterministic_example`;
they are not claims about current upstream state.

Evidence kinds bind both authority and bounded result metadata. Provider
responses carry an HTTP status, a provider result class, and the classification
policy version; request validation carries its result, failure class, and
policy version. Contract and quality assertions carry explicit pass/fail-like
results and policy versions. Health observations carry a correlated state and
probe policy, while provider notices carry their direct state and notice
version. A `ref_id` or a generic version string never establishes a cause by
itself.

## Same symptom, different action

Three examples deliberately reference the same bounded symptom,
`provider-response:http-401`:

| Corroborating evidence | Cause | Recommended action | Avoid |
| --- | --- | --- | --- |
| authoritative operation approval is `approved_pending_sync` | `approval_propagating` | `wait_for_approval_sync` | `reissue_credential` |
| provider explicitly rejects the configured credential | `credential_invalid` | `verify_credential_configuration` | `assume_provider_outage` |
| Healthcheck and a provider notice corroborate unavailability | `provider_outage` | `check_provider_status` | `reissue_credential` |

An HTTP status alone cannot choose among these causes. In particular, the
generic data.go.kr service-key rule remains ambiguous until another evidence
authority narrows it.

Kind-specific authority rules are fail-closed: provider responses originate
from the provider or CLI adapter, request validation from CLI, Registry rules
from Registry, Health observations from Healthcheck, and provider notices from
the provider or its portal. Consumer-owned contract and quality assertions use
the explicitly allowed consumer authority set.

## Scenario coverage

The deterministic examples cover the seven initial user journeys plus the
additional contract, credential, success, and unknown boundaries:

1. `approval_required`
2. `approval_propagating`
3. `invalid_input`
4. `rate_limited`
5. `provider_outage`
6. `semantic_quality` for HTTP success with empty or missing expected data
7. `stale_data` with a versioned freshness policy and actual/reference times
8. `contract_drift`
9. `credential_invalid`
10. `ready`, requiring an operation-scoped passed validation level
11. `unknown`

`ready` is not transport success. It requires a versioned validation result
whose scope is the exact operation and whose achieved level meets or exceeds
the required level.
`stale_data` is not inferred from HTTP age or a display label; it requires a
versioned freshness assertion containing reference time, actual time, maximum
age, and a `stale` result.

A single response classified as `service_unavailable` may support an inferred
outage and `check_provider_status`, but it cannot justify
`avoid=reissue_credential`. That stronger advice requires a versioned Health
correlation or a direct provider notice independent of the credential path.

## Relationship to existing Registry contracts

The draft is additive:

- `datapan.error-action-catalog.v1` remains the provider-signature and internal
  work-routing source.
- `datapan.health-probe-catalog.v1` remains the immutable operation identity and
  bounded probe-policy source.
- Healthcheck remains the owner of observations, aggregation, incidents,
  retention, and public status.
- CLI remains the owner of local credential use, request validation, execution,
  and redacted runtime receipts.
- Web remains the owner of presentation and user interaction.

The later mapping step may reference existing error rules and health evidence;
this draft does not perform runtime inference or change those contracts.

## Versioning and consumer behavior

The draft may change during review. Once v1 is published, its required shape,
enum values, action meanings, and evidence semantics are immutable. Shape or
enum additions require a new schema version because the contract is
fail-closed with `additionalProperties=false`.

Consumers validate the exact `schema_version`. They render explanation and
action identifiers using consumer-owned copy. If evidence does not satisfy a
specific cause, consumers emit `unknown` plus `gather_more_evidence`; they do
not guess a provider outage, approval state, or credential state.

Run the focused contract gate with:

```bash
python3 scripts/validate-diagnostic-envelope-draft.py
python3 -m unittest tests/test_validate_diagnostic_envelope_draft.py
```

The machine-readable review packet is
`drafts/diagnostic-envelope/consumer-contract.v1.json`, and every complete
consumer example is under `drafts/diagnostic-envelope/fixtures/`.
