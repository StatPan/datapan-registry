# GitHub REST merge-identity captures

These local fixtures preserve the response shapes captured for peer PR #720 from the repository's frozen October 4, 2026 API evidence. They are regression data only; they are not a publication receipt, journal entry, or operational authority.

Each envelope records its native source filename, the SHA-256 of that exact captured response JSON, endpoint, and server-selected API version. The response retains the exact PR body, merge time, state, repository IDs/names, base and head refs/SHAs, and `merge_commit_sha` presence or absence. Only unrelated account avatars/URLs, repository metadata, and PR links/counters were removed. The PR body is preserved byte-for-byte at the decoded text level because the recovery code checks it for canonical ownership markers and journal equality.

The 2026-03-10 captures omit `merge_commit_sha`. The paired 2022-11-28 captures include the exact source SHA `849af2936a743573358820275df985b5809d0f7b`. The checked-in fixture bodies are sanitized projections, so their file hashes differ from the recorded native response hashes.
