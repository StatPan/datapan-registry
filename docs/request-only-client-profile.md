# Request-only client profile

`reports/data-go-kr/request-only-client-profile.json` is a Registry-owned,
immutable release artifact derived only from
`reports/data-go-kr/operation-manifest.json`. It is metadata a later consumer
may use to prepare a request description after validating its pins; it is not a
generated client, provider-call permission, retry policy, response schema, or
claim that any operation succeeds.

Every operation-manifest identity is present exactly once. An operation with an
endpoint retains its REST method or SOAP action and parameter descriptors, but
is `unsupported_requires_approved_parameters` until the consumer obtains
approved runtime values outside this contract. An operation without an endpoint
is `unsupported_missing_endpoint`. Consumers must surface either outcome rather
than attempt a request.

The profile binds the source operation-manifest and profile-schema byte SHA-256
values. A raw manifest SHA inside a manifest-bound profile would be circular,
because `manifest.json` records the profile's own SHA. Therefore
`release_manifest.sha256` is the canonical release ledger SHA-256 with only the
profile artifact descriptor omitted; the consumer proof also verifies the raw
profile, schema, and source-manifest SHA-256 entries in `manifest.json`.

Registry-local verification is offline:

```sh
python scripts/generate-data-go-kr-request-only-client-profile.py --check
python scripts/validate-data-go-kr-request-only-client-profile.py
python -m unittest tests/test_validate_data_go_kr_request_only_client_profile.py
```
