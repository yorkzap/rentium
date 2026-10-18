# Shared tenancy facts and unit workspace — October 2026

`leases.tenancy_summary` is the shared read used by lease detail/list APIs,
unit overview, RAMA's bounded tenancy reads and portfolio occupancy tools.
An accepted `MoveOutRequest` is notice evidence even when the document inbox
is empty. Agreement status, expected possession, recorded occupancy, accepted
end date, signature roster, physical scope and deposit status remain separate.
A lease term alone does not establish actual occupancy or vacant possession.

## Durable records and transitions

- `LeaseSpaceScope` records the physical unit and either its whole space or
  selected persistent bedroom-area IDs. Reviewed corrections append versions.
  Resolved scopes freeze when a pending/active lease is saved. Legacy records
  with incomplete layout stay explicitly partial until reviewed.
- `TenancyAssessment` records tenancy-start owner kitchen/bathroom facts. A
  generic landlord/relative/common-space flag cannot establish BC s.4(c).
  Unreviewed BC records retain protective rules and an explicit review warning.
  This assessment does not rewrite the signed agreement or its PDF.
- `TenancyPresence` records a verified possession/departure event. Expected
  dates cannot be entered as future actual events. Existing occupancy logs
  remain visible as recorded evidence, not eyewitness verification.
- `RentalConfiguration` selects dated offerings inside an unchanged physical
  unit. One future transition is approved at a time. Existing listings are
  reused; missing offerings become private drafts based on recorded layout.
  No automatic publishing, rent assignment, inventory duplication or deletion.
- Preview tokens expire after 30 minutes and bind to the unit's current facts.
  Confirmation locks the unit and rejects stale previews. Retries return the
  original receipt. Activation rechecks commitments and departure; blocked
  transitions appear in Today. New commitments cannot overlap space or conflict
  with an approved configuration. Existing unresolved history is not rewritten.
- A fixed-term date alone no longer expires a BC tenancy. Continuing tenancies
  needing a billing review appear in Today. This release does not automatically
  extend a signed term or post extra rent beyond the existing billing schedule.

REST and RAMA's dated `set_unit_rental_mode` use the same configuration service.
The older immediate switch is locked and cannot override dated configurations.
Room bundles can be recorded with selected bedroom IDs in the lease review;
review the scope before issuing an agreement. A scope review is an operational
record, not a legal amendment or a substitute for correct agreement wording.

## User-facing changes

Today, Leases, Money, Properties and Visits & repairs are the primary landlord
navigation. Other existing workspaces remain under More. Unit links open current
and future tenancies, offerings, layout and configuration history. Unit links to
Money, visits and repairs carry their scope. The calendar uses scheduled tenancy
ends instead of labelling every fixed-term expiry a move-out.

RAMA answers the reported occupancy/notice conversation without a provider call.
Default read replies are brief; source links and evidence details remain available.
Missing model credentials no longer hide locally supported tenancy reads.
All model writes continue through the existing persisted preview/confirmation
control plane. This is not a new permission for autonomous money or notice writes.

## Validation and rollout

Full local PostgreSQL suite: 2,449 passed, 2 skipped. Follow-up targeted checks
cover subsequent browser-found fixes. TypeScript, production build and critical
Python static checks pass. Repository-wide historical lint warnings remain.
Browser checks use synthetic local data, including the reported three-party
signing/accepted-notice case and a November room-configuration preview.

Additive migrations: properties 0017–0018, leases 0029–0031. Deploy backend,
migrations and worker before frontend. Keep database, media, code and prior-image
backups. Never infer owner-sharing facts for all legacy leases. Garden Suite's
review is based on the landlord's explicit confirmation of whole-suite scope and
no owner kitchen/bathroom sharing at tenancy start; preserve the lease, notice,
signature and ledger records byte-for-byte at the model-data level.

Legal sources checked for the design:
- https://www.bclaws.gov.bc.ca/civix/document/id/complete/statreg/00_02078_01
- https://www2.gov.bc.ca/gov/content/housing-tenancy/residential-tenancies/calculators-and-resources/tenancy-laws-rules/tenancies-types
