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
→ role/tool allowlist and landlord-scoped resolution
→ read result, clarification, or complete mutation preview
→ explicit confirmation
→ service-layer transaction
→ concrete completion message and audit record
```

High-confidence property rename, dashboard navigation, room listing, and
group-room creation requests bypass speculative planning. The General has
direct access to routine property tools and may still delegate specialized
work. Known supported requests are rejected by `log_capability_gap` with the
matching tool name so they cannot create false capability records.

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

RAMA links come from `rentium.rama.links`, never from model-generated prose.
Registered collection routes include dashboard home, properties, property
groups, documents, leases, finances, maintenance, and settings. Entity routes
use registered templates such as `/dashboard/properties/{id}` and RAMA
document detail.

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
