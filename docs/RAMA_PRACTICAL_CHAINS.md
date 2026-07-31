# RAMA practical chains (landlord real usage)

What you actually ask for in chat should hit a **tool**, not a shrug or a
false capability gap. This map is driven by Telegram transcripts + backend
capabilities (July 2026).

## Chains you already hit (and status)

| You said | Should do | Status |
|---|---|---|
| Invoice photo / OCR / file expense | `catalog_business_document` → `file_business_document` | ✅ |
| Find window screens invoice | `search_business_documents` | ✅ |
| Delete mis-upload | API delete + CASCADE events | ✅ |
| “I bought X for $Y, paid, no receipt” | `create_expense` + `paid_on` | ✅ |
| “Void the wrong $125…” | `void_ledger_entry` (not create) | ✅ |
| “Mark Draino paid” / “not yet taken?” | `mark_ledger_paid` | ✅ deterministic |
| Make viewing + email prospect | `schedule_viewing` | ✅ |
| Change time after preview | amend pending plan / `reschedule_viewing` | ✅ |
| Cancel viewing for Ishupreet | `cancel_viewing` contact= | ✅ |
| Have they seen viewing link? | `viewing_invite_status` | ✅ |
| Has Siya signed / seen the lease? | `tenant_lease_status` | ✅ |

## False gaps previously logged (tools already exist)

| Logged “gap” | Real tool |
|---|---|
| Reschedule viewing | `reschedule_viewing` |
| Create lease draft | `create_lease` |
| Create rooms / listings | `create_group_room`, `create_property_structure` |
| Co-landlord | `add_co_landlord` / `add_co_host_to_lease` |
| Work order multi-room | `create_work_order` (unit/shared scope) |
| Lease invite last-seen | `tenant_lease_status` / `list_lease_roster` |
| Deliver lease PDF in chat | `deliver_lease_pdf` |

`log_capability_gap` should refuse these via `supported_tool_for_request`.

## Next high-value gaps (backend ready-ish)

| Need | Backend | RAMA next step |
|---|---|---|
| Email **delivered/bounced** for invites | SendGrid webhooks | stamp Appointment / LeaseTenant |
| Reallocate expense holding in chat | `reallocate_expense` | ✅ exists — ensure phrase routing |
| Documents UI mark paid / move holding | ledger services | FE buttons |
| Soft-delete documents | model | trash tool |
| Bulk work orders | WO create | batch plan |
| Bank balances update | `update_bank_balance` | already tool; add to GENERAL if missing |
| List import batches | `list_import_batches` | GENERAL opt-in |
| Morning briefing prefs | constitution/memory | clarify tool |

## Learning loop

1. Landlord: “log the gap” / “learn now”  
2. `RamaCapabilityGap` row  
3. Engineer ships tool + `supported_tool_for_request` + roles  
4. Model uses tool; gap marked BUILT  

Preferences (not new capabilities): `remember` / `forget`.

## Deterministic routers (no model required)

| Intent | Function |
|---|---|
| Verbal expense | `_verbal_expense_intent` |
| Void expense | `_void_expense_intent` |
| Schedule viewing | `_schedule_viewing_intent` |
| Amend viewing time on pending plan | `_amend_pending_schedule_from_message` |
| Viewing link opens | `viewing_invite_status` block |
| Lease signed/seen | `tenant_lease_status` block |
| Mark expense paid | `mark_ledger_paid` block |
| Reschedule / cancel viewing | respective blocks |
