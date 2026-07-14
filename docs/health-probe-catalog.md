# Health probe catalog contract

`reports/health-probe-catalog.json` is the Registry-owned, versioned input to one-shot `datapan.health-probe.v1` executions. It binds stable `operation_id` policy keys to current Registry selectors and to the operation-key alias emitted by datapan-cli. Display names and CLI operation keys remain aliases; consumers persist the Registry operation ID as the immutable identity.

An entry is executable only when its eligibility status is `eligible` or `credential_required` and the schema supplies a bounded execution policy. Credential requirements describe only type and scope. Safe parameters are generator constraints, never query values, and request budget is fixed to one. Freshness is `not_asserted` unless an entry opts into an explicit timestamp path, format, timezone, and age bound. A `not_asserted` entry records whether its upstream timestamp contract is not yet reviewed or its response has no stable timestamp; it is never a claim that the data is fresh. Empty data is always an observation, not an outage decision.

The catalog intentionally excludes credentials, generated query values, response rows, mutable receipts, and live health state. A runner supplies credentials and evaluates a request; Datapan Status stores or projects the resulting mutable observations outside the Registry release.

`fixtures/health-probe-catalog/cli-health-probe-v1.json` proves that each reviewed selector resolves to the gateway or registered external-adapter contract used by datapan-cli. `scripts/validate-health-probe-catalog.py` checks Registry resolution, CLI operation-key compatibility, bounded parameters, dependency classification, fixture coverage, mutable-data exclusion, and release-manifest digests.
