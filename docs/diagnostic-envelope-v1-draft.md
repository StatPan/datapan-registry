# Diagnostic Envelope v1 Draft

`datapan.diagnostic-envelope.v1` is the proposed minimum contract shared by
Datapan CLI, Healthcheck, and Web when they explain whether a public-data
operation is usable and, if not, what the user should do next.

This is a review draft. Its schema lives under
`drafts/diagnostic-envelope/`, not the public `schemas/` directory. It is not
listed in `schemas/index.json`, bound by `manifest.json`, staged for public
distribution, or authorized as runtime truth. Publication is a separate goal
step after all three consumers provide compatibility evidence.

The data.go.kr proof is
`drafts/diagnostic-envelope/data-go-kr-evidence-mapping.v1.json`. It binds
existing error rules, source-profile facts, and health policy to the typed
consumer evidence required for each cause. Its deterministic proof engine
validates evidence against the envelope `$defs`, binds it to an exact source,
provider, dataset, and operation subject, excludes stale, expired, or
wrong-authority evidence, and fails conflicts closed to `unknown`. Candidate
signals are structurally prohibited from selecting a cause. Registry does not
receive live receipts or select runtime causes. CLI, Health, and Web producer, scope, timing,
redaction, action, and unknown-fallback obligations are recorded under
`drafts/diagnostic-envelope/consumer-compatibility/`.

The source profile's `key_request_url` is a generic usage guide and is explicitly
rejected as an `approval_required` action target. For an exact `data_go_kr`
subject whose eight-digit dataset ID exists in the pinned canonical Registry,
the mapping derives the exact-host HTTPS dataset detail URL
`https://www.data.go.kr/data/{dataset_id}/openapi.do` as a
`dataset_application_entry`. This is an entry page containing the application
flow, not a direct submission URL. Another source, malformed or absent ID, or an
ID missing from the canonical Registry fails closed to
`unknown/gather_more_evidence`.

Run `python3 scripts/validate-diagnostic-evidence-mapping-draft.py` to verify
pinned inputs, false-positive fallbacks, cause/action compatibility, all three
consumer packets, and the unpublished draft boundary.

## Boundary

Registry owns the stable vocabulary, required evidence shape, responsibility
categories, safe action identifiers, avoidance identifiers, redaction rules,
and deterministic examples. A consumer owns the actual observation, inference,
mutable receipt or history, credential access, telemetry, and UI copy.

An envelope has no field for a credential or secret-derived hash, authorization
header, credential-bearing request URL, raw provider text or URL, request or
response body, response rows, or user identity. Identifier fields accept only
bounded lowercase slugs or colon-separated authority identifiers: slash paths,
URLs, whitespace, and credential-labelled segments are invalid. A schema cannot
recognize whether an otherwise valid opaque slug is secretly sensitive, so
producers still must mint non-secret identifiers and attest the redaction flags
truthfully. `evidence_refs` carry those identifiers and typed metadata instead
of raw evidence.

## Minimum envelope

Every instance identifies when the assessment was made, the source and bounded
subject through stable `source_id` and `provider_id` values, one cause and
determination, the accountable party, recommended and avoided actions, typed
evidence references, and a fail-closed redaction attestation.

There is one certainty axis:

- `observed`: the named authority directly supplied or validated the fact;
- `inferred`: multiple bounded facts support the conclusion, but no authority
  states that conclusion directly;
- `unknown`: available evidence does not justify a more specific cause.

There is no numeric or probability-like confidence field. Consumers must not
turn `inferred` into a probability.

`assessed_at` records the conclusion time. Evidence does not duplicate an
absolute observation timestamp or subject identifier. Instead, its
`timing.observed_age_seconds` is a non-negative offset from `assessed_at`, its
validity state and positive remaining validity are bounded by a versioned
policy, and its scope uses the constant `subject_ref=envelope_subject`. This makes “not after
assessment” and subject binding structural JSON Schema facts instead of
cross-field comparisons that different validators could implement differently.
Every evidence reference also names its authority and a safe version identifier
when the conclusion depends on a policy or contract. Checked-in examples use
fictional fixture identities and declare `fixture.status=deterministic_example`;
they are not claims about current upstream state.

Cause and action evidence must explicitly list `cause`, `determination`, and
`action` in `supports`; a typed payload that only supports scope cannot select a
cause or justify advice. Evidence carrying those supports must be
`current_at_assessment`, never immutable. Runtime observations are capped at a
seven-day contract maximum, with tighter limits of five minutes for a provider
response, fifteen minutes for Health correlation, and one day for a provider
notice. Approval propagation is also operation-bound and current at assessment.
`remaining_validity_seconds=0` means expired and is invalid for
`current_at_assessment`; the minimum accepted value is one second.

Evidence kinds bind both authority and one mutually exclusive bounded result
payload. Provider responses carry an HTTP status, a provider result class, and
the classification policy version; request validation carries its result,
failure class, and policy version. Contract and quality assertions carry
explicit pass/fail-like results and policy versions. Health observations carry
a correlated state and probe policy, while provider notices carry their direct
state and notice version. Each evidence kind has a fixed operation, request,
response, or data-quality scope level; source-level substitutions are invalid.
A `ref_id` or a generic version string never establishes a cause by itself.

Action arrays are also fail-closed. Every cause has one exact recommended action
object and a bounded optional or required avoid action object, including the
actor and rationale identifier. Consumers cannot append `continue_to_reuse` to
an error, advise key reissue for rate limiting, or substitute another actor.

## Same symptom, different action

Three examples deliberately reference the same bounded symptom,
`provider-response:http-401`:

| Corroborating evidence | Cause | Recommended action | Avoid |
| --- | --- | --- | --- |
| authoritative operation approval is `approved_pending_sync` and the same operation has a current unclassified 401/403 response | `approval_propagating` | `wait_for_approval_sync` | `reissue_credential` |
| provider explicitly rejects the configured credential | `credential_invalid` | `verify_credential_configuration` | `assume_provider_outage` |
| Healthcheck and a provider notice corroborate unavailability | `provider_outage` | `check_provider_status` | `reissue_credential` |

An HTTP status alone cannot choose among these causes. In particular, the
generic data.go.kr service-key rule remains ambiguous until another evidence
authority narrows it.
Likewise, an approval record alone cannot establish propagation: the envelope
must also contain the current same-subject failure symptom before advising the
user to wait and avoid key reissue.

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

A single current response classified as `service_unavailable` may support only
an inferred outage and `check_provider_status`, but it cannot justify
`avoid=reissue_credential`. That stronger advice requires a versioned Health
correlation or a direct provider notice independent of the credential path.
A provider outage may be `observed` only when a current direct provider notice
states suspension or degradation; Health correlation and classified responses
remain inferred evidence.

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

Consumers validate the exact `schema_version` with the Draft 2020-12 JSON
Schema. The schema is the only required validation artifact: subject binding,
relative time validity, evidence support, kind-exclusive payloads, outage
determination authority, evidence-kind scope, exact cause/action/actor binding,
safe identifier profiles, and the complete ready-level ordering are all encoded
there. No consumer-specific Python or hidden semantic validator is required.
The schema validates typed authority attestations and their safe composition; it
does not independently prove that an upstream authority reported a true fact or
detect arbitrary secrets disguised as syntactically safe opaque IDs. Consumers
render explanation and action identifiers using consumer-owned copy. If
evidence does not satisfy a specific cause, consumers emit `unknown` plus
`gather_more_evidence`; they do
not guess a provider outage, approval state, or credential state.

Run the focused contract gate with:

```bash
python3 scripts/validate-diagnostic-envelope-draft.py
python3 -m unittest tests/test_validate_diagnostic_envelope_draft.py
```

The machine-readable review packet is
`drafts/diagnostic-envelope/consumer-contract.v1.json`, and every complete
consumer example is under `drafts/diagnostic-envelope/fixtures/`.
