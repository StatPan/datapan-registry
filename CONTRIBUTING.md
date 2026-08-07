# Contributing to Datapan Registry

Thank you for improving the Registry. Please keep changes reviewable and
bounded to a named source, contract, or release-quality need.

## Contribution license

By intentionally submitting a contribution for inclusion in this repository,
you confirm that you have the right to submit it and license that contribution
under the [Apache License 2.0](LICENSE). This is the inbound=outbound rule for
this repository; no separate contributor license agreement is required.

Do not submit upstream data, provider documentation, credentials, or material
whose source terms do not permit the proposed use. A contribution that adds or
changes source evidence must retain the official references and terms boundary
described in [Source rights](docs/source-rights.md).

## Before opening a pull request

- Read the repository operating guidance in [AGENTS.md](AGENTS.md).
- Explain the user or release-quality result, scope, and verification evidence.
- Do not hand-edit generated release artifacts to make a check pass.
- Run the focused checks documented by the changed area. For the governance
  baseline, run `python3 scripts/validate-oss-governance.py`.
- Keep source and provider rights claims factual and linked to the relevant
  source profile or provenance record.

For security-sensitive reports, follow [SECURITY.md](SECURITY.md) instead of
opening a public issue with details.
