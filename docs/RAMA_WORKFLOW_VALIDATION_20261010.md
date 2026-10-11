# Daily workflow release validation — October 10, 2026

Backend implementation: `6651407`; frontend: `4d0862a`.

## Automated and live checks

- Full backend suite: **2,359 passed, 2 skipped** against disposable PostgreSQL.
- Forward migration, reverse migration and reapplication of `rama.0032`: passed.
  Final `makemigrations --check --dry-run`: no changes detected.
- New production modules and standalone eval scripts: Ruff passed. Existing
  repository-wide lint debt is not covered by this statement.
- Frontend TypeScript, targeted ESLint and production build: passed.
- Desktop approval/resume/persisted-receipt and mobile failure/width browser
  checks: **2 passed** against a disposable portfolio.
- Live **OpenAI GPT-5 mini** acceptance gate: **40/40 passed**, five attempts
  each for expense, payment, onboarding, move proposal, listing changes,
  composed changes, ambiguous target and unsupported bank transfer.
- After adding durable address-reply continuation to the ambiguity guard,
  all **five additional live ambiguity attempts passed**. The unit regression
  also verifies that replying with an address resumes the saved complete plan
  without another provider call.

The full live run checks persisted approvals, no preapproval domain effects,
expected actions and exact record counts, database postconditions, verified
receipts and repeated-approval idempotency. The final run had no critical
failures. It uses synthetic owners and rolls each scenario back; email remains
in memory. The unsupported bank-transfer boundary is deterministic, so passing
that scenario does not claim model-mediated banking support.

Earlier live runs exposed and led to fixes for missing action schemas,
independent calls for dependent lease/invite steps, asking-rent routing,
duplicate replacement drafts, unrelated write-tool substitution and model-chosen
IDs bypassing ambiguous listing names. A description test was made explicitly
quoted after sentence-ending punctuation made its expected exact text ambiguous.

This is a bounded scenario gate, not a statistical guarantee across all natural
language, lease types or property configurations. Broader domain coverage comes
from the regression suite; the first real workflow remains a monitored pilot.
Mistral Small was **not certified** because its configured key returned 429s.

## Release boundary

Deploy backend, migration and worker before the frontend. Enable only the main
existing GPT-5 mini portfolio; do not change its provider/key or enable other
owners. Financial and tenancy changes still require the saved complete preview
and explicit approval. Move proposals wait for consent and replacement signing;
onboarding waits for activation and distinguishes queued invitation delivery.

Rollback planning by disabling `workflows_enabled`. Retain task/receipt history
and the additive schema. Deployment backups are under
`/opt/rentium/releases/rama-workflows-20261010`; the prior application image is
`rentium_oracle_django:pre-rama-workflows-20261010`. Database dump and media archive
contents were verified before the switch. Never remove persistent volumes or
reverse the schema after new workflow evidence exists.
