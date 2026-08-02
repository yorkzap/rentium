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
4. Checks mappings against the General's fail-closed capability contract, not
   merely registered internal tools.
5. Reports **covered**, **missing_tool**, **unmapped**, and **intentional**.

`--fail-on-gap` fails for a mapped capability unavailable to the General or an
unreviewed non-GET endpoint. Read-only list/retrieve noise may remain unmapped;
every scanned mutation must be covered or explicitly intentional.

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
| Rename + preview business documents | Metadata-only `title` update, authenticated blob preview, and confirmed `rename_business_document` with strict amount/date/holding disambiguation |
| Mark document expense paid | `POST /api/rama/documents/<id>/mark-paid/` + Documents UI |
| Move document holding (+ reallocate expense) | `POST /api/rama/documents/<id>/move/` + Documents UI |
| Soft-delete / trash / restore | `deleted_at`; `DELETE` soft; `POST …/restore/`; list `?status=TRASH` |
| Bulk tag / trash / restore / move | `POST /api/rama/documents/bulk/` |
| Email delivery/bounce | `Appointment.invite_email_*` + send stamp + `POST /api/public/email-events/` |
| Reallocate chat phrases | Expanded `supported_tool_for_request` → `reallocate_expense` |
| Parity map reocr/tags/actions | `COVERAGE_MAP` entries for document_* views |

## Current landlord mutation coverage (August 2026)

The parity gate currently has no missing tools and no unmapped mutation
endpoints. Recent composites cover:

- document tags/trash/restore/re-OCR/move/mark-paid;
- listing-media ordering, property groups/common areas/shared inventory;
- full unlocked lease fields and exact lease-roster row edits;
- all appointment kinds, viewing-window edit/removal, and reversible manual
  calendar archive/restore;
- condition-report headers/custom rows/keys;
- import upload/mapping/row correction/reversible exclusion;
- showcase, insight, notification-channel, and consented Treasurer settings;
- explicitly saved, parameterised workflows that always re-preview.

Read-only public/auth/tenant-self-service endpoints and irreversible account,
credential, or permanent-record deletion remain intentional exclusions.

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
| Cleaning deposit paid | `mark_cleaning_deposit_paid` |
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
- Provider/API-key/account controls and internal capability-gap triage

## Rules

- Composites call the **same services** as the REST views (no parallel
  business rules).
- Preview / `confirm=yes` is mandatory for money and legal actions.
- Ledger voids/corrections are **append-only** (`void_entry` / `correct_entry`).
- Capability-gap logging must not claim “unsupported” for phrases that
  already map via `supported_tool_for_request`.
