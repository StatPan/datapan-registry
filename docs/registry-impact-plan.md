# Registry Impact Plan

Registry releases should be evidence led, but evidence alone is not enough for
downstream systems. Consumers need to know whether a registry change means
"regenerate examples", "refresh provider verification", "review a promoted
collector", or "review a database migration".

`datapan.registry-impact-plan.v1` defines that bridge while keeping this
repository as a source/spec/evidence ledger, not an execution runtime.

The impact plan should be generated from source profiles informed by
`docs/source-standardization-research.md`, not from assumptions copied from a
single provider.

## Artifact

Future release drafts should generate:

```text
reports/registry-impact-plan.json
schemas/datapan.registry-impact-plan.v1.schema.json
```

For multi-source releases, source-specific plans may also be generated:

```text
reports/data-go-kr/registry-impact-plan.json
reports/open-assembly/registry-impact-plan.json
```

Root-level `reports/registry-impact-plan.json` is the release-wide rollup.

The schema is checked in at
`schemas/datapan.registry-impact-plan.v1.schema.json`, but the current release
manifest is not updated by this design step. The schema should be added to
`schemas/index.json` and `manifest.json` by a future generated release draft.

## Stable Identity

Impact records must join on canonical source and endpoint identity, not display
names.

Required identity fields:

- `provider`: upstream provider label, such as `data.go.kr`.
- `source_id`: stable source identifier, such as `data_go_kr`.
- `endpoint_id`: stable endpoint identity, such as
  `data_go_kr:15084084:getVilageFcst`.

Optional identity fields:

- `list_id`: upstream list, dataset, or catalog id.
- `operation_id`: upstream operation id or normalized operation name.
- `dataset_key`: promoted Datapan dataset key when one exists.
- `host`: endpoint host.

Registry-only additions should still get stable identities even when no
downstream dataset is promoted.

## Change Categories

The v1 contract supports these categories:

- `added`
- `removed`
- `deprecated`
- `endpoint_changed`
- `required_params_changed`
- `response_shape_changed`
- `cadence_changed`
- `provider_error_taxonomy_changed`
- `verification_status_changed`

The category describes the source/spec change. It does not by itself imply a
database migration, SDK rebuild, or adapter edit.

## Downstream Action Hints

Each change includes one or more action hints:

- `target`: `datapan-cli`, `datapan-data`, `dataset-api`, `sdk`, `mcp`,
  `provider-adapter`, `registry-release`, or `documentation`.
- `action`: `no_action`, `regenerate`, `refresh_verification`,
  `review_collector`, `review_parser`, `review_schema`,
  `db_migration_review`, `update_adapter`, `update_error_taxonomy`,
  `deprecate`, `remove`, or `investigate`.
- `automation`: `automated`, `manual_review`, or `blocked`.
- `reason`: human-readable explanation for the action.

This keeps the registry independent from downstream implementation details
while still making release impact machine-readable.

## Promoted Dataset Boundary

The plan must distinguish registry coverage from promoted dataset contracts:

- `promoted_dataset: false`, `served_dataset: false`: registry-only change.
  Downstream `datapan-data`, Dataset API, SDK, and MCP targets may use
  `no_action`.
- `promoted_dataset: true`: a Datapan data block exists and collector/parser
  review may be needed.
- `served_dataset: true`: Dataset API/OpenAPI/SDK/MCP contracts may need
  regeneration.
- `response_shape_changed` on a promoted dataset may require
  `db_migration_review`.

This prevents a large registry import from creating false-positive work in
runtime repositories.

## Error Taxonomy Actions

Provider errors should be classified before routing:

- `credential`: key missing, expired, unauthorized, or approval required.
- `rate_limit`: throttling or quota exhaustion.
- `provider_contract`: upstream response/error contract changed.
- `upstream_outage`: provider outage, timeout, DNS, 5xx, or dead route.
- `parser`: response still valid but parser logic is wrong.
- `adapter`: site-specific adapter behavior needs a code change.
- `unknown`: insufficient evidence.

`provider_error_taxonomy_changed` should normally produce one of:

- `update_error_taxonomy` for registry metadata updates.
- `refresh_verification` when evidence is stale.
- `update_adapter` when a site-specific adapter can handle the failure.
- `investigate` when the classification is `unknown`.

## Examples

Registry-only addition:

```json
{
  "identity": {
    "provider": "data.go.kr",
    "source_id": "data_go_kr",
    "list_id": "15084084",
    "operation_id": "getVilageFcst",
    "endpoint_id": "data_go_kr:15084084:getVilageFcst"
  },
  "category": "added",
  "severity": "info",
  "promoted_dataset": false,
  "served_dataset": false,
  "actions": [
    {
      "target": "datapan-data",
      "action": "no_action",
      "automation": "automated",
      "reason": "Registry addition is not promoted as a Datapan data block."
    }
  ]
}
```

Promoted response shape change:

```json
{
  "identity": {
    "provider": "data.go.kr",
    "source_id": "data_go_kr",
    "list_id": "15084084",
    "operation_id": "getVilageFcst",
    "endpoint_id": "data_go_kr:15084084:getVilageFcst",
    "dataset_key": "weather_vilage_forecast"
  },
  "category": "response_shape_changed",
  "severity": "high",
  "promoted_dataset": true,
  "served_dataset": true,
  "actions": [
    {
      "target": "datapan-data",
      "action": "db_migration_review",
      "automation": "manual_review",
      "reason": "Promoted dataset response fingerprint changed."
    },
    {
      "target": "dataset-api",
      "action": "regenerate",
      "automation": "manual_review",
      "reason": "Served dataset contract may expose changed fields."
    }
  ]
}
```

## Consumer Expectations

`datapan-cli` should generate the impact plan from registry diffs, verification
evidence, provider profiles, and promoted dataset mappings supplied by
downstream repositories.

`datapan-data` should consume the plan as input to collector/parser/schema
review. It should not be required to parse the full registry release to decide
whether promoted datasets are impacted.

Dataset API, SDK, and MCP generators should act only on changes marked
`served_dataset: true` or on explicit action hints for their target.

## Non-Goals

- Do not encode Airflow, dbt, database, billing, account, or customer-specific
  state in the registry impact plan.
- Do not make registry releases depend on downstream repository internals.
- Do not call upstream providers while reading the impact plan.
