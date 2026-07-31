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

## Phase 1 composites (July 2026)

| API | RAMA tool |
|---|---|
| `LeaseViewSet.renew` | `renew_lease` |
| `MoveOutViewSet.create` / `settle_deposit` | `settle_moveout` |
| `ConditionInspectionViewSet` package | `complete_inspection_package` |
| `RentAdjustmentViewSet.create` | `apply_rent_adjustment` |
| `POST /api/ledger/utility-bills/` | `record_utility_bill` |
| `InquiryViewSet.to_appointment` | `convert_inquiry_to_viewing` |

All six are confirm-gated write tools on the General + Corporal surfaces,
wired through `domain_composites.py` → `tools.py` → `REGISTRY`.

## Rules

- Composites call the **same services** as the REST views (no parallel
  business rules).
- Preview / `confirm=yes` is mandatory for money and legal actions.
- Capability-gap logging must not claim “unsupported” for phrases that
  already map via `supported_tool_for_request`.
