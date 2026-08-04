# Current Rentium architecture

This note describes the architecture as of July 2026. It supersedes older
module lists where they conflict, while the deeper RAMA and operations guides
remain useful for implementation detail.

## Runtime boundaries

- Django REST Framework owns authenticated business APIs and the small,
  explicitly listed public API surface.
- Next.js owns all browser-facing application and marketing routes. The
  canonical frontend origin defaults to `https://www.rentium.ca`.
- PostgreSQL is the system of record. Redis backs Celery and Django caching.
- Celery handles messaging, document analysis, geocoding, event replay,
  scheduled finance work, SLA checks, and RAMA background analysis.
- Local development runs the backend in Docker Compose. Django source and
  local media are bind-mounted from the backend checkout. Production media
  uses S3-compatible Django storage.
- `api.rentium.ca` reaches the locally hosted backend through the Cloudflare
  tunnel. `app.rentium.ca` is legacy redirect input and is never emitted by
  RAMA.

## Property hierarchy

```text
PropertyHolding (physical/legal/financial address)
└── PropertyUnit (a floor, suite or household — the physical space)
    ├── Property (the OFFERING: one complete-unit listing, or one per room)
    │   └── InventoryItem (private listing inventory)
    ├── PropertyArea (the unit's internal layout — bedrooms, bathrooms, kitchen)
    └── PropertyGroup (present only when the unit is let room by room)
```

Three ideas that used to be one, and are now deliberately separate:

| Concept | Question it answers |
| --- | --- |
| `PropertyUnit` | What physically exists? |
| `PropertyUnit.rental_mode` | How is it being offered right now? |
| lease scope (`Lease.property` / `Lease.group`) | What does a given tenancy cover? |

Before `PropertyUnit` existed there was no level between the address and the
listing, so a floor let as one home and a floor let room by room were both
stored as a `PropertyGroup` full of room listings. Nothing could distinguish
"a 3-bedroom floor let to one family" from "3 rooms let to 3 strangers", and a
9-unit portfolio reported as 14 rooms.

**A bedroom inside a unit is layout, not an offering.** `PropertyArea` records
it regardless of rental mode; only a `BY_ROOM` unit turns bedrooms into
`Property` rows.

`Property.is_active_offering` is false for listings belonging to the mode a
unit is *not* currently in. They are parked, never deleted, so switching back
reuses the original listing with its photos, inventory and history.
`PropertyQuerySet.public()` — the single visibility choke point — excludes
them, and the listing API hides them unless `?include_inactive=true`.

`properties.services` owns rental-mode switching for both REST and RAMA:
`describe_rental_mode_switch` previews without writing, and `set_rental_mode`
refuses while any DRAFT, PENDING or ACTIVE lease exists anywhere in the unit.
`configure_room_offerings` is the composite boundary for converting an existing
whole floor/suite into named room rentals: in one transaction it creates or
reuses the unit's `PropertyGroup`, records the bedroom layout, creates/reuses
the room `Property` offerings, and parks the complete-unit offering. It applies
the same live-lease blocker and never creates a parallel fake suite.
`Lease._validate_no_cross_scope_overlap` blocks the other direction, so a unit
can never be let whole and by the room at the same time.

### Areas

`PropertyArea` is the single area model. A second `Area` model briefly existed
in `properties/areas.py` holding the maintenance and inspection foreign keys;
it never held a row (its seeding signals were never connected) while
PropertyArea held the real data *and* the legally load-bearing
`shared_with_landlord` / `shared_by` fields. The two were merged into
PropertyArea and `Area` was deleted.

An area hangs off exactly one parent — `unit`, `group`, or `property` — enforced
by a check constraint. `serves_areas` records which bedrooms a bathroom is for,
which `shared_by` cannot express because it points at listings.

`is_seeded_default` marks the generic starter set created by
`seed_default_areas` so maintenance and inspections have something to
reference. It is scaffolding, not a fact a landlord stated, and RAMA's layout
reporting excludes it — otherwise "we know nothing about this floor" silently
becomes "it has a garage and a laundry".

## Legal regime follows lease scope

`leases/tenancy_rules.py` resolves how a tenancy legally behaves. The deciding
facts are what the lease **covers** and whether the landlord shares — never how
the unit is currently marketed.

- `lease_covers_whole_unit()` — a COMPLETE_UNIT listing, a group lease whose
  tenants between them hold every room, or a group lease with no per-room
  assignment. A by-room floor where one party takes every room is a whole-unit
  tenancy in law.
- `landlord_shares_common_areas()` reads areas on the listing, its unit and its
  room-group. The unit lookup matters: once layout lives on `PropertyUnit`, a
  landlord-shared kitchen recorded there would otherwise be invisible, and an
  invisible sharing flag means the RTA s.4(c) exemption silently stops being
  applied.
- `TenancyRules` exposes `covers_whole_unit` and `landlord_shares` so the two
  facts that drove the answer are auditable in API responses and in the
  move-out `rules_snapshot`.
- `_jurisdiction()` falls back to the property's province. Every new room lease
  uses the one `GENERIC_ROOMMATE` agreement regardless of province, so a BC
  room tenancy would otherwise fall through to GENERIC and be offered a generic
  mutual-agreement form instead of RTB-8.

"Whole self-contained unit, but the owner shares the kitchen" is unrepresentable
by construction: `Lease.clean` restricts `common_space_shared_with` to roommate
agreement types, and a COMPLETE_UNIT listing may only use a residential type.

Group common areas carry an explicit `is_group_common` marker and a
`shared_with_landlord` legal classification. Joining a room to a group attaches
it to all group common areas. Moving or removing the room safely removes the
old associations and preserves a valid primary area for the remaining group.

## RAMA request path

RAMA is not a free-form chatbot with direct database access. Its request path
is:

```text
Web chat or linked landlord channel
→ deterministic high-confidence intent routing
→ capability retrieval over the role's allowlist
→ landlord-scoped resolution and application service
→ typed answer, clarification, no-op, failure, or complete mutation preview
→ explicit confirmation
→ service-layer transaction
→ immutable action receipt, verification, and concrete completion message
```

`rama/capabilities.py` indexes registered operations by business-language
aliases, descriptions, risk, and schema. A turn sees a small relevant subset
instead of the entire registry; `search_capabilities` lets the model retrieve a
missed operation without pretending it is unsupported. High-confidence
property rename, dashboard navigation, room listing, and group-room creation
requests still bypass speculative planning. The General directly handles
routine leases, invites, payments, viewings, media, and property structure and
may delegate specialist analysis. Known supported requests are rejected by
`log_capability_gap` with the matching operation name.

Routine writes retain the same safety rule everywhere: one complete preview,
then an explicit confirmation using the same arguments. `create_group_room`
creates a room, private inventory, group membership, and shared-area
associations in one transaction. It derives location and holding data only
when every existing group member agrees, asks once for a missing landlord-use
classification, warns about exact and near-duplicate names, and rolls the
whole operation back on failure.

Whole-house descriptions use the atomic `create_house_layout` service rather
than a model-authored sequence of unrelated calls. It can create one holding,
empty or populated property groups, room listings, private areas such as an
ensuite, subset-shared areas such as a bathroom used by two named rooms, and
group-wide areas. Missing location and landlord-use facts produce one
state-backed clarification; the completed hierarchy then receives one preview
and one transaction.

### Command state and receipts

Provider prose is presentation, never workflow state. A confirmed operation is
backed by `RamaTask` and follows a validated FSM:

```text
RECEIVED → NEEDS_INPUT / AWAITING_CONFIRMATION → EXECUTING
         → VERIFIED / FAILED / CANCELLED
```

Pending plans point at their task. Each completed plan step writes one immutable
`RamaActionReceipt` containing the capability, exact inputs, effects, entity
references, verification evidence, and links. Repeated confirmations are
recognized as already applied, and tool-specific `already_done` guards run both
before preview and immediately before execution.

Plan steps also have a stable `step_id`, explicit `depends_on` edges, and typed
result bindings such as
`{"$step": "create-room", "path": "property.id"}`. The runner resolves a
binding only from an earlier verified result, then reruns blockers and duplicate
checks against the resolved IDs immediately before execution. A failed step
skips its dependants; RAMA never guesses a newly created row from its name or
reconstructs a chain after confirmation.

`rama/capability_contract.py` is the fail-closed landlord capability policy.
The parity scanner compares landlord-facing mutating REST actions against the
General's callable allowlist, not merely the registry, and CI fails for a
missing mapped tool or an unreviewed mutation. Permanent property, draft-lease,
and inventory deletion and engineering capability-gap triage remain outside
chat. Reversible document, calendar, insight, notification-channel, import-row,
showcase, shared-inventory, lease-roster, appointment, and Treasurer-setting
operations are exposed through guarded composites.

### Explicit saved workflows

`RamaSavedWorkflow` stores only a landlord-approved sequence of capability keys,
typed parameters, stable step dependencies, and a contract version. RAMA never
learns or executes a chain silently. `save_last_workflow` requires its own
preview and confirmation and rejects files, credentials, tokens, passwords, and
prior confirmation state. Running a saved workflow compiles supplied parameters,
revalidates every current tool schema/blocker, and creates the ordinary complete
pending-plan preview; the landlord must confirm that run normally.

### Attachments and listing media

Chat files are grouped in a conversation-owned `RamaAttachmentBatch`. The
composer sends that exact batch ID with the message; the server seals it, and
media/document tools must target the batch or explicit attachment IDs. There is
no fallback to “all unused uploads,” so a later set of 11 photos cannot absorb
17 earlier photos or a mortgage document from another message.

`properties.media_services` is the shared REST/RAMA boundary for attach,
manifest, reorder, exact removal, and atomic removal of a selected set. Media
manifests use numbered thumbnails and stable handles (`primary`,
`gallery:<id>`). The dashboard and RAMA both delete through those handles, so
one confirmation removes only the selected images. Business documents are
promoted from one exact attachment into
`RamaDocument`; the landlord's stated intent, not the file merely being an
image, determines whether it is a listing photo or a document.

### Roles

Four roles, dispatched from one table in `rama/roles.py`. `ROLE_TOOLS` is
**fail-closed** — an unrecognised role raises rather than falling through to
the full write surface — and `role_allows_tool` gates the deterministic write
routers in `service.py`, which call `registry.execute` directly and would
otherwise bypass the role's tool list entirely.

| Role | Writes | Reachable |
|---|---|---|
| Corporal | Yes, always behind a confirmation | Chat |
| General | Routine operations, planning, policy, and specialist delegation | Chat |
| FSA | No (`READ_ONLY_ROLES`) | Delegation only |
| Treasurer | No (`READ_ONLY_ROLES`) | Chat, delegation, weekly beat |

The two read-only roles are read-only by construction: no tool on their lists
takes a `confirm` argument, so `pending_specs` is provably always empty and no
plan can originate from them. Asserted by test, not by convention.

The Treasurer additionally runs a nine-stage deliberation engine
(`rama/deliberation.py`) in which only GATHER, CHALLENGE and RECOMMEND cost a
model call; enumeration, scoring, ranking and every published figure are
Python, which is what makes its output provider-neutral. See
`RAMA_TREASURER.md`.

RAMA links come from `rentium.rama.links`, never from model-generated prose.
Registered collection routes include dashboard home, properties, property
groups, documents, leases, finances, maintenance, and settings. Entity routes
use registered templates such as `/dashboard/properties/{id}` and RAMA
document detail.
Public listing links use the listing's canonical
`/<province>/<city_slug>/<public_slug>` route and report whether the visibility
rules currently make it live; name-derived `/properties/...` URLs are never
invented.

## Shared application-service boundaries

REST and RAMA are adapters over the same domain functions for workflows that
previously drifted:

- `leases.services.create_lease_record` owns portfolio scope, defaults, model
  validation, and landlord-shared legal-clause derivation.
- `appointments.services.schedule_viewing` owns exact property scope,
  timezone, tenant-consent state, proposal history, and event publication.
- `properties.services.configure_room_offerings` owns complete-unit to by-room
  conversion, exact room names, and the common areas stated in that request.
- `properties.media_services` owns listing media.
- `ledger.services.record_payment` remains the immutable, idempotent settlement
  boundary; a partial payment settles the original charge and leaves its
  computed balance without rewriting that charge.
- `ledger.services.return_refundable_deposits` is the move-out return boundary.
  It derives what was actually received per deposit charge and posts separate,
  idempotent security, pet, and cleaning-deposit return rows.

Lease invitations additionally have an append-only `LeaseInviteEvent` stream:
`SENT`, `LINK_OPENED`, `ACCOUNT_LINKED`, `SIGNED`, `DECLINED`, and `RESENT`.
`LINK_OPENED` means the token-gated URL was opened; it is deliberately not
described as proof that the recipient read the lease. RAMA and the lease API
project the same lifecycle facts.

## Lease form packs (August 2026)

A tenancy is executed against one document — the agreement rendered by
`leases/documents.py`. Form packs are the extra paper a real tenancy needs:
BC's RTB-8, a pet addendum, a guarantor form, a landlord's own PDF.

```text
LeaseFormTemplate          catalogue entry (system form, or a landlord's upload)
  └── LeaseFormPlacement   reusable field boxes, in page fractions
LeaseForm                  one template attached to one lease
  ├── placements_snapshot  frozen at attach time
  ├── LeaseFormSigner      who signs; carries the public sign_token
  ├── LeaseFormSignature   immutable evidence, one per signature
  └── LeaseFormEvent       immutable lifecycle stream
```

**Stage is what makes a form mean something.** `WITH_LEASE` is part of executing
the lease and an unsigned one blocks `check_and_activate()`; `ADDENDUM` is
signed at any point and never blocks; `MOVE_OUT` (RTB-8) ends a tenancy and
drives `MoveOutRequest`. A form attached to an already-ACTIVE lease is recorded
as outstanding and raises an attention item, but never pushes that lease
backwards. `form_services.blocking_forms` is the single source of truth, read by
the FSM, the attention feed, the lease API and RAMA.

**Nothing infers a stage.** `form_intel.suggest_form_purpose` reads OCR text and
form-field names, scores the evidence deterministically, and writes only
`suggested_stage`. A human — in the picker, or by answering RAMA's
`question_for_user` — promotes it. The same rule the document inbox applies to
amounts.

**Rendering.** `form_render` reads geometry and AcroForm widgets with pypdf,
rasterises pages with Ghostscript (already present for OCRmyPDF), and stamps a
reportlab overlay. Output is always flattened: `/AcroForm` and every page's
`/Annots` are removed, so an executed document has no editable layer. AcroForm
widgets are used only to derive suggested placements, never to fill — one code
path for a government form and a scanned addendum. Placements are page
fractions (0..1, top-left origin), the only representation that survives a
browser at arbitrary zoom.

**Signing.** Lease parties sign in-app; anyone else uses `/sign/<sign_token>`, a
single-slot, single-use, expiring capability under `/api/public/`. The lease
itself is still not signable without an account. Every signature stores the
typed legal name, timestamp, IP, user agent, and the checksums of both the blank
form and the values shown — more than the lease-signing path records. On the
last required signature the form is stamped once, hashed, and stored;
`render_form_pdf` returns those bytes forever after, never a re-render.

`send_form` refuses while any required non-signature field is blank: a mutual
agreement to end a tenancy with no end date on it is not a document anyone
should be asked to sign. Signatures fill signature boxes and date-signed boxes
(`auto_source=today`) and nothing else — they never populate an empty name box,
because a single name string cannot honestly fill a form that splits first and
last names.

No lease-form file is served from `FileField.url`; production sets
`AWS_QUERYSTRING_AUTH = False`, so bytes go through auth- or token-gated views.

RAMA reaches all of this through one confirm-gated tool, `manage_lease_forms`
(`risk="legal"`, `own_confirm`, `autonomy=NEVER`). A PDF sent in chat with
signing/lease wording routes to it rather than to `catalog_business_document` —
the third branch in `batch_chat_note` and the attachment focus block.

## Document intelligence

`RamaDocument` is the document inbox record. Uploads preserve the original
file, SHA-256 digest, detected type, processing state, extracted text and
structured facts, review state, and landlord/property/holding scope. Celery
performs OCR, conversion, and analysis; supported office/image inputs can
produce an archival PDF without replacing the original.

Documents are not ledger facts until the review/confirmation boundary is
crossed. Expense creation follows the append-only ledger rules and can be
scoped directly to a holding through `LedgerEntry.holding`. Signed leases,
inspection evidence, and originals are immutable evidence rather than editable
content.

The Documents UI and RAMA may rename only `RamaDocument.title`, the human-facing
library label. The original filename, canonical archive key, file bytes, hash,
filing state, and linked ledger entry are unchanged, and the rename is appended
to `RamaDocumentEvent`. RAMA resolves rename requests with strict combined
selectors (vendor/title/OCR words plus exact amount, and optionally date or
holding), refuses zero/multiple matches, previews the exact record, then stores
its UUID and expected title in the confirmation plan so approval cannot drift
to a similar or newly uploaded document.

The dashboard obtains previews through the authenticated download endpoint and
renders supported images/PDFs without exposing storage URLs. Document cards show
the extracted amount and payment state as first-class metadata. Tagging,
soft-trash/restore, re-OCR, holding moves, and marking a linked expense paid are
available to RAMA through the same document services. A statement with no
attached file is never catalogued as a document: purchase language creates a
ledger expense directly, and a receipt can be attached later if one arrives.

Local document files live below Django `MEDIA_ROOT` in the bind-mounted backend
checkout. Production uses the configured S3 storage backend with overwrite
disabled. Database backup alone is therefore not a complete document backup:
back up both PostgreSQL and the active media store.

## Frontend contract

The frontend consumes API clients rather than constructing backend URLs inside
components. Current canonical application collections include:

- `/dashboard`
- `/dashboard/properties`
- `/dashboard/documents`
- `/dashboard/leases`
- `/dashboard/financial`
- `/dashboard/maintenance`
- `/dashboard/settings`

Property details use `/dashboard/properties/{id}`. Browser auth and middleware
protect dashboard routes, while Django permissions and landlord-scoped
querysets remain the authoritative security boundary.

## Local startup and recovery

The local Compose startup graph is:

```text
PostgreSQL healthy ─┐
                    ├→ migrate exits 0 ─→ Django / Celery / Flower
Redis healthy ──────┘
```

The dedicated migration service ensures schema changes finish before web or
worker code starts. PostgreSQL recovery must preserve the named data volume;
see `POSTGRES_RECOVERY.md` for the backed-up, single-file PID recovery
procedure and verification checklist.


## Maintenance, damage and deposits

A work order belongs to a LISTING or, for a fault in shared space, to a
PropertyUnit — a washroom serving three rooms is nobody's room.
`WorkOrder.objects.for_landlord()` is THE scoping rule (listing OR unit);
nothing may reimplement it. Six hand-written `property__landlord` filters is
exactly how shared-space jobs became invisible the day `property` went
nullable.

`responsible_tenant` + `tenant_chargeable` record who caused damage. Completing
or attributing a chargeable job raises a FEE_CHARGE claim against that tenant,
linked to the work order and to the lease whose deposit it may be claimed
against.

Deposits are never netted automatically — see `ledger.services.deposit_position`
and the RAMA guide for why (BC RTA: written agreement or an RTB application
within 15 days, else the claim is lost and double the deposit is payable).

The Standard Roommate Agreement has a refundable `cleaning_deposit`, never a
cleaning fee. It is a `DEPOSIT_CHARGE`, excluded from income exactly like the
security deposit. A single incoming e-transfer may be allocated across both
open charges, but those allocations remain distinct. On a full move-out return,
the security and cleaning deposits are posted as separate `DEPOSIT_RETURN`
entries. A lease with no cleaning deposit produces no cleaning return.

### The 15-day deposit clock

`MoveOutRequest` records the forwarding address and its arrival date, because
the clock starts on the LATER of the tenancy ending and that address arriving
IN WRITING — and the second date was recorded nowhere, so the deadline could
not be computed at all.

`deposit_deadline` / `days_left_to_settle` / `deposit_status()` expose it, and
`attention.service._deposit_deadlines` puts it in front of the landlord: urgent
inside five days or once passed, and a separate "chase the forwarding address"
item when the tenancy has ended and no address has arrived. A deadline nobody
looks at is not a safeguard.

`deposit_position` reports `claim_deadline` only when the clock has genuinely
started. Deriving one from the end date alone would name a deadline that has
not begun — worse than reporting none, because the landlord would act on it.

Settlement is explicit: RETURNED_IN_FULL, TENANT_AGREED (written) or
RTB_APPLIED. Nothing settles a deposit implicitly.
