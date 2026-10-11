# RAMA daily workflows

## Goal and rollout boundary

RAMA prepares complete, owner-scoped plans for money, existing-offering tenant
onboarding, proposed tenant moves, and property changes. The model interprets the
request; existing domain adapters and services resolve, validate and execute it.
This is an extension of `RamaTask` and `plan_runner`, not another workflow engine.

`RamaPreferences.workflows_enabled` defaults to false. Migration `rama.0032` adds
workflow identity/version, stage, durable progress, parent attempts and the
WAITING/PARTIAL states. Enable one landlord only after the acceptance gate below.
Enabling this flag does not change the landlord's provider or model.

## Execution contract

1. `plan_daily_workflow` accepts up to 12 supported actions. When enabled, the
   model gets this planner instead of individual core write schemas. It can read
   action schemas through capability search; relevant contracts are also supplied
   at the start of the turn. Named capabilities outside the daily pilot retain
   their guarded legacy model interface. Deterministic certified routes and
   the existing onboarding/move convenience tools use the same compiler.
2. Every independent action uses the real domain preview. Names resolve to stable
   IDs in the owner's portfolio. A model-chosen ID does not resolve a duplicated
   listing name: the user's request must identify an address or explicit listing
   ID. New-record dependencies use typed `$step` output
   references; the invitation can refer to the draft lease created before it.
3. The complete effect list is stored with the task and shown in the approval UI.
   Existing episode/prompt-bound confirmation rules remain authoritative. A
   model-supplied `confirm` or execution policy cannot authorize a write.
4. Approval locks the portfolio and plan, checks target snapshots, and revalidates
   each action through the existing adapter. A failed savepoint rolls back that
   action. Its dependants are skipped; earlier committed work is preserved.
5. Registered database verifiers check the resulting entities, amounts, dates,
   scope and domain state. Receipts explicitly distinguish verified evidence
   from legacy adapter results. A partial, queued or failed response cannot be
   promoted to a completed write merely because it lacks an `error` key.
6. Concurrent/repeated approval returns the recorded result. A fresh retry binds
   completed outputs, skips completed actions and asks for new approval for the
   remaining actions. An uncertain action is not automatically replayed.

Snapshots cover target listings, lease terms/participants, units/layout,
expense scope, payment charges and their settlements. Bank reconciliation retains
its stronger domain-owned exact-effects preview and atomic receipt contract.
Existing immutable ledger records are never deleted to repair a workflow.

## Concrete pathways

- **Money:** payments against exact charges, dated expenses, approved bank review
  batches, and rent adjustments through the existing ledger/lease services.
  Asking/advertised rent belongs to a listing, never a tenant's rent adjustment.
- **Onboarding:** draft lease → requested invitation → optional inspection.
  Missing inspection templates block the proposal before any writes. Delivery
  is queued only after the domain transaction commits. An invitation being sent
  does not finish onboarding: progress waits for lease activation.
- **Tenant moves:** resolve the named tenant on the exact source tenancy; prepare
  a replacement draft and propose the shared service's mutual move-out agreement.
  Tenant agreement and normal replacement signing/activation remain outstanding.
  The old lease is not immediately terminated. Balances/deposits remain attached
  to it. Multi-tenant household moves require clarification instead of selecting
  an arbitrary member. Completing/signing the replacement follows normal lease UI.
- **Property:** listing fields, hierarchy creation, unit layout and rental mode
  use the existing adapters/services. Rental-mode switching preserves parked
  offerings and the existing live-lease blockers.

The invitation outbox has a unique execution key. A worker claims once before
calling the existing mail service. Failure or an ambiguous worker crash requires
review; there is no blind automatic resend after an uncertain send. Saved lease
and invitation records remain available. This is not an exactly-once guarantee
from the external mail provider.

## Visible progress and prepared attention

Owner-scoped endpoints:

- `GET /api/rama/tasks/`
- `GET /api/rama/tasks/<id>/`
- `POST /api/rama/tasks/<id>/resume/`

The chat displays stages, individual step outcomes, canonical record links and
fresh-review actions. Completed retries replace old attempts in the active list;
old task records remain available for audit. WAITING/PARTIAL workflows survive
chat episode boundaries while their expired confirmations do not.

Existing domain-event dispatch and the scheduled RAMA sweep prepare deduplicated
in-app attention cards for unresolved bank rows, incomplete tenancies and blocked
workflows. Acknowledgement/dismissal is preserved, and resolved cards are closed.
These cards are excluded from externally delivered morning briefings. Preparing
attention never creates a new financial or tenancy effect.

## Acceptance and release

Run the full backend suite and the focused `test_daily_workflows.py` suite,
including concurrent approval, owner isolation, stale payment previews, persisted
retry references, failed delivery and database postconditions. Validate forward
and reverse migrations on a disposable database. Run frontend TypeScript, lint,
production build and `playwright.rama.config.ts` against the disposable demo
created by `scripts/seed_workflow_demo.py`.

`scripts/eval_daily_workflows.py` exercises actual `run_turn` requests with a real
provider against synthetic portfolios in a `rentium_workflow*` database. Each
case rolls back; email must use Django's in-memory backend. Keep credential JSON
outside the repo with owner-only permissions. The report contains no key.

Run five attempts for each of the eight scenarios. Require at least 98% complete
supported requests and zero authorization, duplicate-write, ownership or false
completion failures. A provider quota error is a failure to meet the gate, not
proof of successful language interpretation. A smoke run is not certification.
Record the provider, model, code revision and results with the release.

Deploy backend/migration/worker before the frontend; then enable only the pilot
owner whose configured model passed. Keep other owners disabled. Monitor saved
partial/waiting tasks and genuine delivery failures. Roll back new planning by
turning the flag off; preserve task, receipt, lease, ledger and media history.
Do not roll the schema backward on production after workflow records exist.
