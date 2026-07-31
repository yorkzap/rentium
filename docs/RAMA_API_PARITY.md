# API ↔ RAMA parity

Landlords should not hit “I can’t do that in chat” for operations the
dashboard already supports. This document tracks how we measure and close
that gap.

## Automated report

```bash
# Docker
docker compose -f docker-compose.local.yml exec -T django \
  python /app/scripts/rama_api_parity_report.py

# Machine-readable + fail CI if curated map points at missing tools
docker compose -f docker-compose.local.yml exec -T django \
  python /app/scripts/rama_api_parity_report.py --fail-on-gap --json

# Write markdown snapshot
docker compose -f docker-compose.local.yml exec -T django \
  python /app/scripts/rama_api_parity_report.py \
  --out /app/docs/_generated/rama_api_parity.md
```

The script:

1. Scans landlord-facing DRF viewsets / `@api_view` endpoints.
2. Lists every tool in `rama.registry.REGISTRY`.
3. Joins them via a curated `COVERAGE_MAP` in
   `scripts/rama_api_parity_report.py`.
4. Reports **covered**, **missing_tool** (map points at a tool that does not
   exist), **unmapped** (API action not yet reviewed), and **intentional**.

When you add a composite, update `COVERAGE_MAP` in the same PR.

## Status model

| Status | Meaning |
|---|---|
| **covered** | Curated map points at registered RAMA tool(s) |
| **missing_tool** | Map names a tool that is not in REGISTRY — fix immediately |
| **intentional** | Explicitly out of chat scope (auth, public, webhooks, tenant-only) |
| **unmapped** | Not yet reviewed; many are list/retrieve noise |

## Phase 1 composites (July 2026)

| API | RAMA tool |
|---|---|
| `LeaseViewSet.renew` | `renew_lease` |
| `MoveOutViewSet.create` / `settle_deposit` / cancel / decline | `settle_moveout` |
| `ConditionInspectionViewSet` package | `complete_inspection_package` |
| `RentAdjustmentViewSet.create` | `apply_rent_adjustment` |
| `POST /api/ledger/utility-bills/` | `record_utility_bill` |
| `InquiryViewSet.to_appointment` | `convert_inquiry_to_viewing` |

## Viewing invite open tracking (July 2026)

| Need | Implementation |
|---|---|
| “Have they seen the viewing link?” | `Appointment.prospect_link_*` stamped on `GET /api/public/viewing-status/<token>/` |
| RAMA | `viewing_invite_status` (+ fields on `list_appointments`) |
| Calendar UI | Shows open count / last opened on day appointments |

Not email-pixel tracking (unreliable); **status page loads only**.

## Document library + invite delivery (gap-close, July 2026)

| Gap | Implementation |
|---|---|
| Mark document expense paid | `POST /api/rama/documents/<id>/mark-paid/` + Documents UI |
| Move document holding (+ reallocate expense) | `POST /api/rama/documents/<id>/move/` + Documents UI |
| Soft-delete / trash / restore | `deleted_at`; `DELETE` soft; `POST …/restore/`; list `?status=TRASH` |
| Bulk tag / trash / restore / move | `POST /api/rama/documents/bulk/` |
| Email delivery/bounce | `Appointment.invite_email_*` + send stamp + `POST /api/public/email-events/` |
| Reallocate chat phrases | Expanded `supported_tool_for_request` → `reallocate_expense` |
| Parity map reocr/tags/actions | `COVERAGE_MAP` entries for document_* views |

## Remaining product gaps (prioritise)

| Gap | Notes |
|---|---|
| Reschedule emails when only ends_at changes | Mostly works via `appointment.rescheduled` |
| Saved document views | Phase C |
| Prospect email open (pixel) | Explicitly deferred; page-load tracking is enough for v1 |
| Auto-purge trash after N days | Soft-delete exists; purge job optional |

## Learning as we go

RAMA does **not** silently rewrite production code from chat. Learning path:

1. `log_capability_gap` (or “learn now”) → `RamaCapabilityGap` backlog  
2. Engineers build a tool/service + tests  
3. `supported_tool_for_request` + roles so the model uses it  
4. Optional: `remember` for landlord preferences (not new capabilities)

False “I can’t” should hit `supported_tool_for_request` and refuse to log a gap when a tool already exists.

## Gap-close batch (July 2026)

| API | RAMA tool |
|---|---|
| `LedgerEntryViewSet.void` | `void_ledger_entry` |
| `LedgerEntryViewSet.mark_paid` | `mark_ledger_paid` |
| `LedgerEntryViewSet.correct` | `correct_ledger_entry` |
| `LedgerEntryViewSet.credit` | `post_ledger_credit` |
| `LedgerEntryViewSet.charge` | `post_one_off_charge` |
| Inspection items/suggestions/delivered | `update_inspection_items`, `approve_inspection_suggestion`, `dismiss_inspection_suggestion`, `mark_inspection_delivered` |
| Appointment destroy / cancel | `cancel_viewing` |
| Appointment confirm/counter/decline | `respond_to_viewing_request` |
| Payment reminders | `create_payment_reminder`, `mark_payment_reminder_sent`, `list_payment_reminders` |
| Cleaning fee paid | `mark_cleaning_fee_paid` |
| Inquiry notes/archive | `update_inquiry` |
| Import commit/discard | `commit_import_batch`, `discard_import_batch` |
| Notifications | `list_notifications`, `mark_notifications_read` |

Implementation: `rentium/rama/domain_gap_tools.py` + extensions in
`domain_composites.py`.

## Intentional non-coverage

- Public marketing / SEO endpoints  
- Auth, registration, password reset  
- Webhooks (Telegram/WhatsApp)  
- Tenant-only actions (tenant sign, claim, tenant_respond)  
- RAMA HTTP meta (chat, settings, uploads) — already tools elsewhere  
- Hard-delete of permanent records (work orders cancel via transition)  
- Agenda CRUD — primary scheduling is appointments/calendar  
- Showcase settings slug editor — rare; use dashboard  

## Rules

- Composites call the **same services** as the REST views (no parallel
  business rules).
- Preview / `confirm=yes` is mandatory for money and legal actions.
- Ledger voids/corrections are **append-only** (`void_entry` / `correct_entry`).
- Capability-gap logging must not claim “unsupported” for phrases that
  already map via `supported_tool_for_request`.
