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

The current hierarchy is:

```text
PropertyHolding (physical/legal/financial address)
└── PropertyGroup (household or shared unit)
    ├── Property (rentable room or complete unit)
    │   └── InventoryItem (private listing inventory)
    └── PropertyArea (group common area shared by member listings)
```

`Property` remains the rentable listing consumed by leases and showcase APIs.
A holding groups listings at one real-world asset for expenses, documents, and
bank/ledger reporting. A property group represents rooms that share household
space. `properties.services` is the shared implementation for REST and RAMA
group-area operations.

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
