# Frozen publication-acknowledgement incident

These sanitized fixtures preserve the evidence used to reproduce the failed acknowledgement for publisher run `37199628001`, attempt `1`.

- Publisher: `.github/workflows/huggingface-distribution.yml`, workflow ID `311133646`, `workflow_dispatch`, completed successfully at `2026-10-04T11:43:38Z` on head `e34062309a48b0e0b6c0f38add32f0cdec088616`.
- Failed acknowledgement: `.github/workflows/canonical-update-publication-ack.yml`, workflow ID `373708873`, run `37199709258`, attempt `1`, `workflow_run`, failed at `2026-10-04T11:44:09Z`.
- Publication receipt artifact: `huggingface-registry-publication-receipts`, artifact ID `11301727720`, 1,090 bytes. Its verified source is `6a5138c792f4b7402da0c5ab439646bd752a307f`, manifest SHA-256 is `71a5ad4716ef5847210e2aeb8513ec136619e9521e757af106fdb3d048c33e50`, and immutable payload and pointer revisions are `5028b46f19b1730b0b9b56fa968825f7a7dcb90c` and `d53085285e1eaa5ab894a45514e4220eb8725be9`.
- `publisher-run.json`, `publisher-jobs.json`, `publisher-artifacts.json`, `failed-ack-run.json`, and `failed-ack-jobs.json` are captured API metadata for those exact runs. `pre-recovery-journal.json` is the four-record durable journal snapshot before acknowledgement recovery. The receipt, source binding, source manifest, and ZIP are the original publisher artifact contents.

The fixture excludes credentials and signed download URLs. It is test input, not current workflow or publication authority. Verify the checked-in bytes with:

```sh
sha256sum -c SHA256SUMS
```
