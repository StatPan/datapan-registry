# Public-data completeness proof rollup

As of `2026-10-04T13:23:00Z`. This report covers only the registered scopes below; it does not claim to cover all Korean public data.

Evaluation mode: `repository`.

Registered scopes: 9; states: complete=0, partial=0, unknown=0, blocked=9.

Local profiles and candidate denominators are inventory context, not authoritative source-wide denominator evidence. Missing v1 proofs are shown as blocked or unknown without synthetic hashes, timestamps, or counts.

| Scope | Resource kind | Local identities | Proof state | Complete | Current | Updated | Missing evidence |
| --- | --- | ---: | --- | --- | --- | --- | --- |
| curated-payload.rows | payload_rows | unknown | blocked | False | False | False | payload_row_identity_authority_missing (StatPan/datapan-registry #630) |
| curated-payload.snapshot-portfolio | curated_payload_snapshot | unknown | blocked | False | False | False | curated_payload_portfolio_registration_missing (StatPan/datapan-registry #630) |
| data-go-kr.api-catalog | api_catalog_metadata | unknown | blocked | False | False | False | authoritative_catalog_snapshot_missing (StatPan/datapan-data StatPan/datapan-data#1190) |
| data-go-kr.api-operations | api_operation_manifest | 12662 | blocked | False | False | False | authoritative_catalog_observation_missing (StatPan/datapan-data StatPan/datapan-data#1190) |
| data-go-kr.file-datasets | file_dataset | unknown | blocked | False | False | False | authoritative_file_dataset_snapshot_missing (StatPan/datapan-registry #609) |
| ecos.api-operations | api_operation_manifest | 1 | blocked | False | False | False | authoritative_source_identity_set_missing (StatPan/datapan-registry #630) |
| kosis.api-operations | api_operation_manifest | 1 | blocked | False | False | False | authoritative_source_identity_set_missing (StatPan/datapan-registry #630) |
| open-assembly.api-operations | api_operation_manifest | 1 | blocked | False | False | False | authoritative_source_identity_set_missing (StatPan/datapan-registry #630) |
| seoul-open-data.api-operations | api_operation_manifest | 1 | blocked | False | False | False | authoritative_source_identity_set_missing (StatPan/datapan-registry #630) |

## Evidence facets

Facet states describe only the recorded evidence subject. Historical publication/read-back does not imply current release applicability; payload equivalence is shown separately.

- **curated-payload.rows / payload_authority** — `blocked`.
  Details: `{"data_catalog_receipt_is_not_row_evidence":true,"evidence_owner":"StatPan/datapan-data"}`; evidence: none; missing: payload_row_identity_authority_missing.
- **curated-payload.rows / immutable_publication_read_back** — `missing`.
  Details: `{"consumer_read_back_required_for_updated":true}`; evidence: none; missing: same_subject_publication_read_back_missing.
- **curated-payload.snapshot-portfolio / payload_authority** — `blocked`.
  Details: `{"data_catalog_receipt_is_not_row_evidence":true,"evidence_owner":"StatPan/datapan-data"}`; evidence: none; missing: curated_payload_portfolio_registration_missing.
- **curated-payload.snapshot-portfolio / immutable_publication_read_back** — `missing`.
  Details: `{"consumer_read_back_required_for_updated":true}`; evidence: none; missing: same_subject_publication_read_back_missing.
- **data-go-kr.api-catalog / source_authority** — `unknown`.
  Details: `{"authority_state":"unknown","local_inventory_is_not_authority":true}`; evidence: none; missing: authoritative_catalog_snapshot_missing.
- **data-go-kr.api-catalog / immutable_publication_read_back** — `missing`.
  Details: `{"consumer_read_back_required_for_updated":true}`; evidence: none; missing: same_subject_publication_read_back_missing.
- **data-go-kr.api-operations / import_durability** — `historical`.
  Details: `{"attempt":null,"current_subject_applicable":false,"historical_manifest_sha256":"3507d67912a40106c8e48dfd19bb978d3d37b2a9e9a10e571e5fb42734e395d3","historical_operation_manifest_sha256":"931be9cfcc4aa2ce0b444221de2044b624e6dbdcff8af1288be295b7d202f04a","merge_commit":"47a0f6e71d4d73734670bc3da9b64d94f0339f1d","merged_at":"2026-09-23T06:51:42Z","run_id":"35798122454"}`; evidence: reports/runtime-freshness-import-admissions/35798122454.json, reports/runtime-freshness-imports/35798122454.json, reports/runtime-freshness-import-attestations/35798122454.json; missing: current_contract_import_missing.
- **data-go-kr.api-operations / terminal_execution** — `blocked`.
  Details: `{"percentage_promotion_allowed":false}`; evidence: none; missing: terminal_execution_coverage_missing.
- **data-go-kr.api-operations / source_observation** — `historical`.
  Details: `{"attempt":1,"candidate_sha256":"90bc22e0ad61dd3672b09b7c52e4898be5afb535f87a8d7fa9e5e5c983e6e3b5","observation_authenticated":true,"observed_at":"2026-09-29T23:44:39Z","refresh_evidence_sha256":"5b940ae0ffc71826a7aefe2de3a1453d6c28c67de9cd506c06be073d936e6197","run_id":"36646768289","step_completed_at":"2026-09-29T23:47:49Z"}`; evidence: reports/completeness-proof-evidence/issue-659/source/collector-run.json, reports/completeness-proof-evidence/issue-659/source/collector-artifact.zip; missing: post_repair_observation_baseline_unbound.
- **data-go-kr.api-operations / specification_pipeline** — `historical`.
  Details: `{"candidate_bytes":139155499,"candidate_sha256":"0520d0db0d9ee07b7cbccce0c08439d0b02be901bf10e8491187d96e59d7a0d0","current_input_contract_changed_paths":[".github/workflows/upstream-catalog-refresh.yml",".github/workflows/upstream-catalogue-process.yml","contracts/provider-operation-declarations/data-go-kr-15056854-historical-subject-0085.v1.json","contracts/provider-operation-declarations/data-go-kr-15056854-oa-109-search-last-train-time.v1.json","schemas/datapan.catalogue-composition-receipt.v1.schema.json","schemas/datapan.catalogue-enrichment-evidence.v1.schema.json","schemas/datapan.specs.v1.schema.json","schemas/datapan.upstream-catalogue-checkpoint.v1.schema.json","scripts/compose-upstream-catalogue-candidate.py","scripts/generate-batch-link-detail-registry-patches.py","scripts/generate-seoul-oa109-subject-snapshot.py","scripts/process-upstream-catalogue-candidate.py","scripts/seoul_oa109_operation_declaration.py","scripts/upstream_catalogue_handoff.py"],"current_input_contract_compatible":false,"detail_retry_count":908,"detail_unattempted_count":884,"full_scope_fresh":null,"health_candidate_relation_valid":true,"health_last_good_source_sha":"6a5138c792f4b7402da0c5ab439646bd752a307f","health_main_manifest_sha256":"c0e3708881fb39815205b7358d70ef9c7c0b712422d073589d8edff95f16d54e","health_main_matches_current_subject":false,"health_main_payload_matches_current":true,"health_main_release_manifest_matches_current":false,"health_main_revision":"01148419bef5d7f212e92ab96d0ba1c3da6c298c","health_observation_count":1,"health_processor_observation_matches":true,"health_promotion_execution_matches":true,"health_receipt_sha256":"37a79f569435549f9315336c59a06fad165aff7954fd5f4ef5a2ce86a7ef95be","health_run_id":"37205341388","health_source_observation_matches":true,"pending_count":4382,"processor_generation_id":"8c56b9ad08719bcf99ed9a99d488c26e4bfdf517023a9ca01a548b5d2167e8f9","processor_run_attempt":1,"processor_run_id":"37204681028","promotion_candidate_acknowledgement_statuses":[],"promotion_candidate_available":false,"promotion_candidate_key":null,"promotion_candidate_lifecycle_status":null,"promotion_journal_sha256":"9c992fbcaa05c071a37509622f0e396775e60102b6f22efd555ad849b04875cf","promotion_processor_generation_matches":true,"promotion_run_id":"37205271032","publication_allowed":null,"source_observed_at":"2026-09-29T23:44:39Z","source_run_id":"36646768289"}`; evidence: reports/completeness-proof-evidence/issue-659/health/archive.zip, reports/completeness-proof-evidence/issue-659/health/receipt-blob-api.json, reports/completeness-proof-evidence/issue-659/health/run.json, reports/completeness-proof-evidence/issue-659/health/state-blob-api.json, reports/completeness-proof-evidence/issue-659/processor/archive.zip, reports/completeness-proof-evidence/issue-659/processor/generation-api.json, reports/completeness-proof-evidence/issue-659/processor/index-api.json, reports/completeness-proof-evidence/issue-659/processor/run.json, reports/completeness-proof-evidence/issue-659/promotion/journal-blob-api.json, reports/completeness-proof-evidence/issue-659/promotion/log.txt.gz, reports/completeness-proof-evidence/issue-659/promotion/run.json, reports/completeness-proof-evidence/issue-659/publication/acknowledgement-jobs.json, reports/completeness-proof-evidence/issue-659/publication/acknowledgement-journal-after.json, reports/completeness-proof-evidence/issue-659/publication/acknowledgement-journal-before.json, reports/completeness-proof-evidence/issue-659/publication/acknowledgement-logs.zip, reports/completeness-proof-evidence/issue-659/publication/acknowledgement-run.json, reports/completeness-proof-evidence/issue-659/publication/publisher-anonymous-manifest.json, reports/completeness-proof-evidence/issue-659/publication/publisher-artifacts.json, reports/completeness-proof-evidence/issue-659/publication/publisher-jobs.json, reports/completeness-proof-evidence/issue-659/publication/publisher-pointer-after.json, reports/completeness-proof-evidence/issue-659/publication/publisher-pointer-before.json, reports/completeness-proof-evidence/issue-659/publication/publisher-pointer-immutable.json, reports/completeness-proof-evidence/issue-659/publication/publisher-readback-result.json, reports/completeness-proof-evidence/issue-659/publication/publisher-receipt.json, reports/completeness-proof-evidence/issue-659/publication/publisher-receipts.zip, reports/completeness-proof-evidence/issue-659/publication/publisher-repo-metadata-after.json, reports/completeness-proof-evidence/issue-659/publication/publisher-repo-metadata-before.json, reports/completeness-proof-evidence/issue-659/publication/publisher-run.json, reports/completeness-proof-evidence/issue-659/publication/publisher-source-binding.json, reports/completeness-proof-evidence/issue-659/publication/publisher-source-commit.json, reports/completeness-proof-evidence/issue-659/publication/publisher-source-manifest.json, reports/completeness-proof-evidence/issue-659/publication/publisher-workflow.json, reports/completeness-proof-evidence/issue-659/source/collector-artifact.zip, reports/completeness-proof-evidence/issue-659/source/collector-run.json; missing: post_fix_source_and_complete_scope_acceptance_pending.
- **data-go-kr.api-operations / health_observation** — `historical`.
  Details: `{"attempt":1,"health_is_not_new_source_observation":true,"observation_count":1,"post_state_sha256":"55a4c2a1f78833785961875c287aebf67270333fea534f4c654d6f2a91c8032a","pre_state_sha256":"3bde52aa9fde3e511bc00c040356966ee7608e82775ad7015e6ca3fda6bafcf1","receipt_seal":"1789b7574de56797cc68a75770a28ca55070a0cdaab533a769de4f62cd3bc860","receipt_sha256":"37a79f569435549f9315336c59a06fad165aff7954fd5f4ef5a2ce86a7ef95be","run_id":"37205341388","state_commit":"a7a2347953010052b316675d879e41e0c7982f65"}`; evidence: reports/completeness-proof-evidence/issue-659/health/run.json, reports/completeness-proof-evidence/issue-659/health/archive.zip, reports/completeness-proof-evidence/issue-659/health/state-blob-api.json, reports/completeness-proof-evidence/issue-659/health/receipt-blob-api.json; missing: health_receipt_does_not_establish_additional_source_observations.
- **data-go-kr.api-operations / immutable_publication_read_back** — `historical`.
  Details: `{"acknowledgement_attempt":2,"acknowledgement_job_completed_at":"2026-10-04T12:35:16Z","acknowledgement_job_started_at":"2026-10-04T12:34:46Z","acknowledgement_journal_observed_at":"2026-10-04T12:35:04.198997Z","acknowledgement_log_result_at":"2026-10-04T12:35:14.892701Z","acknowledgement_log_sha256":"11d9495f1a01e41a62145e0b2f90ab9396b377365c82b0be6a4535a2fcb70780","anonymous_readback_observed_at":"2026-10-04T11:48:04.144691Z","candidate_release_publication_unproven":false,"classification":"historical_delivery_only","consumer_read_back_required_for_updated":true,"current_release_manifest_sha256":"eef514c397b1e432cafd5ba5316c708336b061da25301a9e4b2a33cf48457c32","current_release_subject_applicable":false,"currentness_established":false,"cutover_established":false,"evaluation_epoch":"2026-10-04T13:23:00Z","payload_equivalent_to_current_registry":true,"publisher_anonymous_verify_step_completed_at":"2026-10-04T11:43:34Z","publisher_artifact_available_at_evaluation":true,"publisher_artifact_expires_at":"2026-11-03T11:43:34Z","publisher_artifact_sha256":"b7adeada021bbdd1449b7d03b3d73c4094d8213844c42f9a81e0bf1207f05227","publisher_job_completed_at":"2026-10-04T11:43:37Z","publisher_job_started_at":"2026-10-04T11:42:15Z","publisher_publish_step_completed_at":"2026-10-04T11:43:15Z","receipt_cutover_required":false,"receipt_sha256":"6c14adf8f8717f939f6e901f2f4d135055f558a22c1edda0f90e95942280b88d","release_authority":false,"subject":{"acknowledgement_attempt":2,"acknowledgement_run_id":37199709258,"candidate_generation_id":"66a2ae130fce7463dfbdd47caa48d3b7013e47a365a9a2745fe22b5378cd77d4","candidate_head_sha":"8b086826fb1e30ea949cfb8222a252e7aa09fa40","manifest_sha256":"71a5ad4716ef5847210e2aeb8513ec136619e9521e757af106fdb3d048c33e50","payload_revision":"5028b46f19b1730b0b9b56fa968825f7a7dcb90c","pointer_revision":"d53085285e1eaa5ab894a45514e4220eb8725be9","publisher_attempt":1,"publisher_head_sha":"e34062309a48b0e0b6c0f38add32f0cdec088616","publisher_run_id":37199628001,"pull_request":686,"registry_bytes":139155499,"registry_path":"data/data-go-kr.registry.json","registry_sha256":"0520d0db0d9ee07b7cbccce0c08439d0b02be901bf10e8491187d96e59d7a0d0","repository":"StatPan/datapan-registry","source_sha":"6a5138c792f4b7402da0c5ab439646bd752a307f","source_tree_sha":"ff6a93bed060e8bd9ef2738efdc2672d228f35f8"},"updated_claim_established":false}`; evidence: reports/completeness-proof-evidence/issue-659/publication/publisher-run.json, reports/completeness-proof-evidence/issue-659/publication/publisher-jobs.json, reports/completeness-proof-evidence/issue-659/publication/publisher-artifacts.json, reports/completeness-proof-evidence/issue-659/publication/publisher-receipts.zip, reports/completeness-proof-evidence/issue-659/publication/publisher-receipt.json, reports/completeness-proof-evidence/issue-659/publication/publisher-source-binding.json, reports/completeness-proof-evidence/issue-659/publication/publisher-source-commit.json, reports/completeness-proof-evidence/issue-659/publication/publisher-source-manifest.json, reports/completeness-proof-evidence/issue-659/publication/publisher-workflow.json, reports/completeness-proof-evidence/issue-659/publication/publisher-repo-metadata-before.json, reports/completeness-proof-evidence/issue-659/publication/publisher-pointer-before.json, reports/completeness-proof-evidence/issue-659/publication/publisher-repo-metadata-after.json, reports/completeness-proof-evidence/issue-659/publication/publisher-pointer-after.json, reports/completeness-proof-evidence/issue-659/publication/publisher-pointer-immutable.json, reports/completeness-proof-evidence/issue-659/publication/publisher-anonymous-manifest.json, reports/completeness-proof-evidence/issue-659/publication/publisher-readback-result.json, reports/completeness-proof-evidence/issue-659/publication/acknowledgement-run.json, reports/completeness-proof-evidence/issue-659/publication/acknowledgement-jobs.json, reports/completeness-proof-evidence/issue-659/publication/acknowledgement-logs.zip, reports/completeness-proof-evidence/issue-659/publication/acknowledgement-journal-before.json, reports/completeness-proof-evidence/issue-659/publication/acknowledgement-journal-after.json; missing: current_release_publication_read_back_missing.
- **data-go-kr.api-operations / receipt_cutover** — `missing`.
  Details: `{"cutover_active":false,"native_publication_scope_requires_cutover":false}`; evidence: none; missing: producer_receipt_cutover_evidence_missing.
- **data-go-kr.file-datasets / source_authority** — `blocked`.
  Details: `{"authority_state":"unavailable","local_inventory_is_not_authority":true}`; evidence: none; missing: authoritative_file_dataset_snapshot_missing.
- **data-go-kr.file-datasets / immutable_publication_read_back** — `missing`.
  Details: `{"consumer_read_back_required_for_updated":true}`; evidence: none; missing: same_subject_publication_read_back_missing.
- **ecos.api-operations / import_durability** — `missing`.
  Details: `{}`; evidence: none; missing: durable_import_attestation_missing.
- **ecos.api-operations / terminal_execution** — `unknown`.
  Details: `{"local_operation_catalog_is_not_execution_evidence":true}`; evidence: none; missing: source_execution_evidence_missing.
- **ecos.api-operations / specification_pipeline** — `missing`.
  Details: `{"missing_stages":[],"validated_present_stage_evidence":[]}`; evidence: none; missing: authenticated_b_c_health_chain_missing.
- **ecos.api-operations / immutable_publication_read_back** — `missing`.
  Details: `{"acknowledgement_local":null,"consumer_read_back_required_for_updated":true,"missing_stages":[],"publisher_attempt":null,"publisher_readback":null,"receipt_cutover_required":false}`; evidence: none; missing: same_subject_publication_read_back_missing.
- **ecos.api-operations / source_observation** — `missing`.
  Details: `{}`; evidence: none; missing: new_authenticated_source_observation_missing.
- **ecos.api-operations / health_observation** — `missing`.
  Details: `{"health_is_not_source_observation":true}`; evidence: none; missing: health_producer_receipt_missing.
- **ecos.api-operations / receipt_cutover** — `missing`.
  Details: `{"cutover_active":false,"native_publication_scope_requires_cutover":false}`; evidence: none; missing: producer_receipt_cutover_evidence_missing.
- **kosis.api-operations / import_durability** — `missing`.
  Details: `{}`; evidence: none; missing: durable_import_attestation_missing.
- **kosis.api-operations / terminal_execution** — `unknown`.
  Details: `{"local_operation_catalog_is_not_execution_evidence":true}`; evidence: none; missing: source_execution_evidence_missing.
- **kosis.api-operations / specification_pipeline** — `missing`.
  Details: `{"missing_stages":[],"validated_present_stage_evidence":[]}`; evidence: none; missing: authenticated_b_c_health_chain_missing.
- **kosis.api-operations / immutable_publication_read_back** — `missing`.
  Details: `{"acknowledgement_local":null,"consumer_read_back_required_for_updated":true,"missing_stages":[],"publisher_attempt":null,"publisher_readback":null,"receipt_cutover_required":false}`; evidence: none; missing: same_subject_publication_read_back_missing.
- **kosis.api-operations / source_observation** — `missing`.
  Details: `{}`; evidence: none; missing: new_authenticated_source_observation_missing.
- **kosis.api-operations / health_observation** — `missing`.
  Details: `{"health_is_not_source_observation":true}`; evidence: none; missing: health_producer_receipt_missing.
- **kosis.api-operations / receipt_cutover** — `missing`.
  Details: `{"cutover_active":false,"native_publication_scope_requires_cutover":false}`; evidence: none; missing: producer_receipt_cutover_evidence_missing.
- **open-assembly.api-operations / import_durability** — `missing`.
  Details: `{}`; evidence: none; missing: durable_import_attestation_missing.
- **open-assembly.api-operations / terminal_execution** — `unknown`.
  Details: `{"local_operation_catalog_is_not_execution_evidence":true}`; evidence: none; missing: source_execution_evidence_missing.
- **open-assembly.api-operations / specification_pipeline** — `missing`.
  Details: `{"missing_stages":[],"validated_present_stage_evidence":[]}`; evidence: none; missing: authenticated_b_c_health_chain_missing.
- **open-assembly.api-operations / immutable_publication_read_back** — `missing`.
  Details: `{"acknowledgement_local":null,"consumer_read_back_required_for_updated":true,"missing_stages":[],"publisher_attempt":null,"publisher_readback":null,"receipt_cutover_required":false}`; evidence: none; missing: same_subject_publication_read_back_missing.
- **open-assembly.api-operations / source_observation** — `missing`.
  Details: `{}`; evidence: none; missing: new_authenticated_source_observation_missing.
- **open-assembly.api-operations / health_observation** — `missing`.
  Details: `{"health_is_not_source_observation":true}`; evidence: none; missing: health_producer_receipt_missing.
- **open-assembly.api-operations / receipt_cutover** — `missing`.
  Details: `{"cutover_active":false,"native_publication_scope_requires_cutover":false}`; evidence: none; missing: producer_receipt_cutover_evidence_missing.
- **seoul-open-data.api-operations / import_durability** — `missing`.
  Details: `{}`; evidence: none; missing: durable_import_attestation_missing.
- **seoul-open-data.api-operations / terminal_execution** — `unknown`.
  Details: `{"local_operation_catalog_is_not_execution_evidence":true}`; evidence: none; missing: source_execution_evidence_missing.
- **seoul-open-data.api-operations / specification_pipeline** — `missing`.
  Details: `{"missing_stages":[],"validated_present_stage_evidence":[]}`; evidence: none; missing: authenticated_b_c_health_chain_missing.
- **seoul-open-data.api-operations / immutable_publication_read_back** — `missing`.
  Details: `{"acknowledgement_local":null,"consumer_read_back_required_for_updated":true,"missing_stages":[],"publisher_attempt":null,"publisher_readback":null,"receipt_cutover_required":false}`; evidence: none; missing: same_subject_publication_read_back_missing.
- **seoul-open-data.api-operations / source_observation** — `missing`.
  Details: `{}`; evidence: none; missing: new_authenticated_source_observation_missing.
- **seoul-open-data.api-operations / health_observation** — `missing`.
  Details: `{"health_is_not_source_observation":true}`; evidence: none; missing: health_producer_receipt_missing.
- **seoul-open-data.api-operations / receipt_cutover** — `missing`.
  Details: `{"cutover_active":false,"native_publication_scope_requires_cutover":false}`; evidence: none; missing: producer_receipt_cutover_evidence_missing.

Input index semantic identity: `canonical-semantic-json:reports/completeness-proof-inputs.json` has SHA-256 `8c4dba7fd137ce6fd71e78d0a0efb580f5cc5365a68f6f0f65cce57fff41614e` over canonicalized JSON; this is not the raw input file digest.

`updated` requires the existing #631 import-durability, immutable-publication, and consumer-read-back predicates for the same subject. Producer-receipt cutover (#592) is a separate facet and is not silently activated here.

A historical import or scheduled B/C/Health no-op remains historical evidence. It does not add a new source observation or satisfy the original #634 post-fix scheduled-chain and same-subject read-back acceptance by itself.
