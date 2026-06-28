# Source Standardization Research

Datapan should not invent a registry standard from `data.go.kr` alone. Korean
public data sources vary by catalogue model, authentication, endpoint shape,
response format, error vocabulary, pagination, and operational notices. The
standardization path is to keep official source references close to the
registry contracts and let repeated source surveys harden the schema.

This document is a seed research ledger. It should be updated from official
source pages before each source importer or provider profile is implemented.

## Reference Policy

Every source profile should carry reference URLs that can be checked for drift:

- `homepage_url`: human landing page for the source.
- `api_docs_url`: official API documentation or API list.
- `key_request_url`: official key or application page when separate.
- `notice_url`: official notices or service status page when available.
- `terms_url`: official terms, license, or use policy when available.
- `last_reviewed_at`: date when a maintainer checked the references.

Use official source pages as the primary reference. Community libraries are
useful for implementation clues, but they must not override the official
contract.

## Initial Source Survey

| Source | Candidate `source_id` | Official reference | Early profile concerns |
| --- | --- | --- | --- |
| Public Data Portal | `data_go_kr` | https://www.data.go.kr/ | Large gateway catalogue, service-key auth, mixed XML/JSON, many approval-required APIs, external endpoint spillover, provider-specific adapters. |
| KOSIS | `kosis` | https://kosis.kr/openapi/ | Statistical catalogue model, key application flow, multiple output formats including JSON/XML/SDMX/XLS, table and metadata APIs. |
| ECOS | `ecos` | https://ecos.bok.or.kr/api/ | Economic statistics model, key-issued API, path-like query parameters, statistical series metadata, response format variance. |
| Open Assembly | `open_assembly` | https://open.assembly.go.kr/portal/openapi/main.do | Assembly-specific OpenAPI catalogue, key issuance, legislative domain identifiers, source notices tied to parliamentary sessions. |
| Seoul Open Data Plaza | `seoul_open_data` | https://data.seoul.go.kr/ | City portal with OpenAPI, SHEET, FILE, LOD, CHART, MAP, and LINK distributions; frequent service notices and dataset lifecycle changes. |
| Smart Seoul Map | `smart_seoul_map` | https://map.seoul.go.kr/smgis2/openApi | Map-specific API families, geospatial response types, separate application path, POI category APIs. |

The first implementation batch should cover at least one statistical source
(`kosis` or `ecos`), one government/legislative source (`open_assembly`), and
one municipal source (`seoul_open_data`). That gives enough variation to avoid
overfitting the registry to `data.go.kr`.

## Existing Libraries To Inspect

These projects are useful prior art for request construction, parameter naming,
and response normalization. Treat them as implementation references, not source
truth.

| Library | Ecosystem | References | Useful for |
| --- | --- | --- | --- |
| PublicDataReader | Python | https://github.com/WooilJeong/PublicDataReader | Public Data Portal, KOSIS, ECOS access patterns, key handling, tabular cleanup. |
| `kosis` | R | https://cran.r-project.org/web/packages/kosis/readme/README.html | KOSIS service view codes and statistical table access patterns. |
| `ecos` | R | https://cran.r-project.org/web/packages/ecos/readme/README.html | ECOS statistical series access and response handling. |

Library inspection should answer:

- Which official endpoints are wrapped?
- Which request parameters are required by the wrapper?
- How are keys injected?
- How are errors surfaced?
- Does the wrapper normalize responses into tabular records?
- Does it encode service-specific codes that should become registry metadata?

## External Standards To Reuse

Datapan registry schemas should stay pragmatic JSON, but the model should map
cleanly to existing data and API standards where possible.

| Standard | Reference | How Datapan should use it |
| --- | --- | --- |
| JSON Schema 2020-12 | https://json-schema.org/draft/2020-12 | Validate Datapan release artifacts and provider/source profile contracts. |
| OpenAPI Specification | https://www.openapis.org/ | Express normalized callable HTTP API contracts when a source can be projected into endpoint/operation form. |
| W3C DCAT 3 | https://www.w3.org/TR/vocab-dcat-3/ | Align catalogue/dataset/distribution terminology for public data metadata. |
| DCAT-AP | https://op.europa.eu/en/web/eu-vocabularies/dcat-ap | Reference public-sector data portal metadata profiles and cross-portal search concepts. |
| Frictionless Table Schema | https://frictionlessdata.io/specs/table-schema/ | Reference tabular field typing and portable table schema ideas for CSV/XLS/table outputs. |

Datapan does not need to become RDF-first or OpenAPI-only. It should preserve
source reality, then expose mappings where they are reliable.

## Source Profile Fields To Prove

Before freezing `datapan.provider-profile.v1`, survey each source against these
candidate fields:

- identity: `source_id`, `provider`, `homepage_url`, official references.
- catalogue: list endpoint, search endpoint, dataset detail endpoint, update
  cadence, notice page.
- auth: none, key, approval-required, per-service approval, key placement,
  key parameter name.
- request: HTTP method, path template, query parameter model, required
  parameters, paging parameters, date/range parameters.
- response: JSON/XML/CSV/XLS/SDMX/HTML, encoding, result envelope, item path,
  total count path.
- error taxonomy: status code field, message field, auth failure codes, quota
  codes, maintenance codes, upstream outage signatures.
- operation lifecycle: active, deprecated, removed, approval-required,
  service-root-only, external endpoint, malformed, unsupported protocol.
- runtime policy: timeout, retry, backoff, rate limit, sample parameters,
  verification mode.
- downstream mapping: registry-only, promoted dataset, served dataset,
  collector/parser/schema owner.

Fields should graduate into schema only after at least three materially
different sources need them.

## Drift Checks

Reference drift is itself a registry signal. A future scheduled check should:

- fetch official reference URLs and record HTTP status, final URL, content hash,
  and checked timestamp;
- flag changed API docs, key application pages, notices, and terms pages;
- attach source-specific drift evidence under `reports/<source>/`;
- feed `provider_error_taxonomy_changed`, `endpoint_changed`, or
  `verification_status_changed` entries into the registry impact plan when the
  change affects downstream consumers.

Do not fail a release only because a homepage changed. Fail or warn based on
the affected contract: API docs, key policy, endpoint shape, response shape, or
verification evidence.

## Open Questions

- Should source profile references be checked into `providers/<source>.json` or
  a separate `sources/<source>.json` inventory?
- Should official notice pages be scraped as source evidence, or only linked
  and manually reviewed?
- How should source profiles represent API families that share a homepage but
  have separate key policies?
- Which promoted dataset mapping file should provide `dataset_key` for impact
  plans without coupling this repository to `datapan-data` internals?
