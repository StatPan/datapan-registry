# Health runtime observation plan

`reports/health-runtime-observation-plan.v1.json` is static Registry policy, not
a command or a live receipt. It contains exactly eight immutable memberships.
The plan's `manifest_binding.sha256` is SHA-256 of canonical UTF-8 JSON with
sorted keys and compact separators after removing only `bytes` and `sha256`
from its own manifest artifact entry. Its path, kind, schema, and every other
manifest entry remain covered. The full `manifest.json` separately binds the
exact plan bytes and SHA-256. This breaks the unavoidable self-reference while
remaining fail-closed on any other manifest or plan-entry change.

Health verifies the plan bytes/SHA and binding before execution; it owns all
credentials, execution, retention, and live status. No plan member includes a
credential value, query value, full URL, command, response, or live result.
