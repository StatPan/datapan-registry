# Registry operation observation plans

The observation-plan compiler enumerates API operation identities already registered in Registry and separates three independent states: request-plan completeness, runtime binding, and admission. A complete request plan describes how a bounded read-only request is shaped. It does not bind a production credential, prove entitlement, reserve a provider quota, or admit a live call.

The current source set derives its denominator from five pinned scopes: the data.go.kr operation manifest and four registered supported-operation denominators. The data.go.kr manifest contributes its exact API operation identities; the four other sources contribute the operation IDs that Registry has actually registered while each scope remains `inventory_unknown=true`. Each operation carries the exact source-profile `adapter_id` separately from its Registry `source_id`. Provider-index adapter entries are host/adapter metadata, not operation identities. data.go.kr link operations are retained as a separate exclusion count and are not API request plans. The compiler checks exact ID sets, duplicate identities, supported exclusion keys, protocol/status arithmetic, and artifact hashes rather than using a historical operation total as its denominator.

Each index shard contains at most 256 plans, sorted by operation ID. `identity_set_sha256` is SHA-256 over the UTF-8 bytes of a compact JSON array containing the lexically sorted operation ID strings, with `ensure_ascii=false`, separators `,` and `:`, and no trailing newline. The release `manifest.json` binds the index and every shard by path, byte count, and SHA-256; the index in turn binds the source manifest, source snapshots, source profiles, and denominator artifacts. Consumers must verify the release-manifest entry before following the index's shard references.

## Request authority

An incomplete plan remains non-admitted and names the missing fields. A production request plan may be complete only when digest-bound evidence establishes the operation-specific HTTP method and read-only effect, transport and endpoint, complete parameter inventory, each parameter's location and cardinality, an explicit bounded value strategy, authentication placement, request limits, and a response assertion with explicit empty-result semantics. SOAP plans state the HTTP method separately from the SOAP action and include the SOAP version, envelope namespace, operation QName, body encoding, and any SOAP-header authentication QName. SOAP action text alone does not establish a read-only operation.

Value strategies are explicit and evidence-backed. A bounded integer has minimum, maximum, and selection semantics. A relative year has an anchor, offset, and allowed year bounds. Reviewed enum/literal values are either present as reviewed non-secret values or referenced by an exact digest-bound evidence pointer. Credential values never appear in Registry plan records; credentials are represented only by runtime references in a private runtime binding.

The ten existing Health canary selectors and their reviewed bounded-parameter strategies are copied into `legacy_policy` records with their exact policy pointers. The compiler does not infer required/optional cardinality from an empty parameter list and does not promote `registry_default_get` to method authority. Operation-document sidecars contribute only individually digest-bound facts with source locators; unknown or service-level-only method evidence remains unknown, and document evidence alone does not create a complete request plan.

## Runtime binding and quota scopes

A bound runtime policy has an opaque credential-store reference when authentication requires one, a `credential_scope_key` that matches exactly one credential quota policy, one or more explicit quota policies, observation period, and evidence references. `source_binding.adapter_id` tells a consumer which registered adapter is allowed to resolve that reference; consumers must not infer credential names from environment variables. No-auth plans carry neither a credential reference nor a credential scope key. Quota scope keys are stable non-secret bucket identifiers shared by every operation governed by the same budget. Each constraint carries its own limits; provider-wide, shared-credential, organization, and API limits can all apply to one operation. Consumers reserve all applicable scopes atomically. Different limits for the same `(scope_kind, scope_key)` are a validation error.

Quota digests are `SHA256(UTF8("datapan.quota-scope.v1\\0" + scope_kind + "\\0" + scope_key))`. For example, scope kind `credential` and scope key `synthetic:credential-group:local-test` produce `9edfd346f33fb2c2b2308b096260bb16a9c5dc8910c216095c7f23fb3cc78ddf`. A scope key is never a credential value, a credential hash, or a secret-derived identifier. Source counts such as request counts and historical canary budgets are not quota limits.

Admission requires a complete request plan and bound runtime policy plus admission evidence. An unknown provider inventory scope does not turn a known registered operation into an unknown ID, and it does not imply that the whole provider catalog is covered. Missing source evidence, runtime bindings, and admission are reported independently.

## Regeneration and verification

```sh
# First commit the compiler, schemas, parser, and sanitized document evidence.
# Then generate output against that immutable source commit:
python scripts/generate-operation-observation-plan.py --source-revision <compiler-source-commit-sha>
python scripts/register-operation-observation-plan-artifacts.py --write
python scripts/sync-release-schema-artifacts.py --write
python scripts/generate-operation-observation-plan.py --check
python scripts/register-operation-observation-plan-artifacts.py --check
python scripts/sync-release-schema-artifacts.py --check
python -m unittest tests/test_operation_observation_plan_fixtures.py
```

The checked-in synthetic REST and SOAP plans use `.invalid` hosts and an explicit `test_only=true` source binding plus method, read-only, input, authentication, quota, limit, and response declarations. They exercise compiler and consumer contracts without contacting a provider. The current set contains 12,666 currently known registered API-operation IDs: 12,662 data.go.kr rows from a source-complete manifest and four known IDs across four partial source scopes whose upstream inventory remains unknown. All current plans remain incomplete, all runtime bindings are unbound, and all operations are not admitted. The 8,871 data.go.kr links and 138 provider-index adapter entries remain separate from API-operation identities. This is inventory reconciliation and execution groundwork, not proof that every Registry source is enumerated or live observation coverage.
