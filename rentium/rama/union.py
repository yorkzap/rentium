"""
"State of the Union": a service-layer aggregate of the whole portfolio —
money this month, outstanding/overdue, deposits held, open work, and what
needs attention. Built exactly like ledger's summary_view and useful on the
dashboard before any AI touches it — which is the test every RAMA component
must pass: useful without the model, safer with it.
"""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

from django.db.models import Count, Sum


def _month_bounds(day: date) -> tuple[date, date]:
    start = day.replace(day=1)
    end = date(start.year + start.month // 12, start.month % 12 + 1, 1)
    return start, end


def _serialize_expense(entry) -> dict:
    prop_name = entry.property.name if entry.property_id else None
    try:
        cat_label = entry.get_category_display() if entry.category else ""
    except Exception:  # noqa: BLE001
        cat_label = entry.category or ""
    bank = getattr(entry, "bank_status", None) or (
        "PAID" if entry.paid_on else "NOT_YET_TAKEN"
    )
    return {
        "id": str(entry.pk),
        "amount": str(entry.amount),
        "description": entry.description,
        "category": entry.category or "",
        "category_display": cat_label,
        "vendor": entry.vendor or "",
        "property": prop_name,
        "property_id": str(entry.property_id) if entry.property_id else None,
        "holding_id": str(entry.holding_id) if entry.holding_id else None,
        "effective_date": entry.effective_date.isoformat()
        if entry.effective_date
        else None,
        "paid_on": entry.paid_on.isoformat() if entry.paid_on else None,
        "bank_status": bank,
        "bank_status_label": (
            "Taken from bank" if bank == "PAID" else "Not yet taken from bank"
        ),
    }


def month_money(landlord, start: date, end: date, *, property_id=None) -> dict:
    """Expected vs collected income, expenses, and net for [start, end).

    Same queryset math as ledger's summary_view, for a single month —
    deposits stay out of income (refundable liability) but are reported.
    Includes expense line items so RAMA can answer "$600 expense" without guessing.
    """
    from rentium.ledger import services
    from rentium.ledger.models import INCOME_CHARGE_TYPES, EntryType, LedgerEntry

    live = LedgerEntry.objects.filter(landlord=landlord).not_voided()
    if property_id:
        live = live.filter(property_id=property_id)

    expected = live.filter(
        entry_type__in=INCOME_CHARGE_TYPES, due_date__gte=start, due_date__lt=end
    ).aggregate(s=Sum("amount"))["s"] or Decimal("0.00")

    collected = live.filter(
        entry_type=EntryType.PAYMENT,
        settles__entry_type__in=INCOME_CHARGE_TYPES,
        effective_date__gte=start,
        effective_date__lt=end,
    ).aggregate(s=Sum("amount"))["s"] or Decimal("0.00")

    expense_qs = (
        live.filter(
            entry_type=EntryType.EXPENSE,
            effective_date__gte=start,
            effective_date__lt=end,
        )
        .select_related("property")
        .order_by("effective_date", "created_at")
    )
    expense_rows = [_serialize_expense(e) for e in expense_qs]
    spent = sum((Decimal(r["amount"]) for r in expense_rows), Decimal("0.00"))
    not_yet = sum(
        (
            Decimal(r["amount"])
            for r in expense_rows
            if r["bank_status"] == "NOT_YET_TAKEN"
        ),
        Decimal("0.00"),
    )

    deposits_in = services.deposits_collected_between(
        landlord, start, end, property_id=property_id
    )

    return {
        "month": start.strftime("%Y-%m"),
        "label": start.strftime("%b %Y"),
        "as_of_year_hint": (
            f"This is {start.strftime('%B %Y')} — use this year, never invent a past year."
        ),
        "expected_income": str(expected),
        "collected_income": str(collected),
        "expenses": str(spent),
        "expenses_not_yet_taken_from_bank": str(not_yet),
        "expense_count": len(expense_rows),
        "expense_lines": expense_rows,
        "net": str(collected - spent),
        "deposits_collected": str(deposits_in),
    }


def list_expenses(
    landlord,
    *,
    month: str = "",
    day: str = "",
    property_query: str = "",
    amount: str = "",
    limit: int = 50,
) -> dict:
    """List landlord EXPENSE ledger lines.

    Filters: month (YYYY-MM), day (YYYY-MM-DD), property name fragment, amount.
    If the user asks about expenses "today" and none fall on that calendar day,
    still return this month's expenses under ``this_month_expenses`` so the
    model can answer helpfully instead of only "none today".
    """
    from django.db.models import Q

    from rentium.ledger.models import EntryType, LedgerEntry
    from rentium.properties.models import Property

    today = date.today()
    day_s = (day or "").strip()
    day_filter: date | None = None
    if day_s:
        try:
            day_filter = date.fromisoformat(day_s)
        except ValueError:
            return {
                "error": f"day must be YYYY-MM-DD, got {day!r}",
                "as_of": today.isoformat(),
                "expenses": [],
            }

    if month:
        try:
            year, mon = month.split("-")
            start, end = _month_bounds(date(int(year), int(mon), 1))
        except (ValueError, TypeError):
            return {
                "error": f"month must look like 2026-07, got {month!r}",
                "as_of": today.isoformat(),
                "expenses": [],
            }
    elif day_filter:
        # Start with that day; may widen if empty (see below).
        start, end = day_filter, day_filter + timedelta(days=1)
    else:
        # Default: ~3 months back through end of current month
        start = today.replace(day=1)
        m = start.month - 2
        y = start.year
        if m <= 0:
            m += 12
            y -= 1
        start = date(y, m, 1)
        end = date(today.year + (today.month // 12), (today.month % 12) + 1, 1)

    def _base_qs(date_start, date_end):
        q = (
            LedgerEntry.objects.filter(landlord=landlord)
            .not_voided()
            .filter(entry_type=EntryType.EXPENSE)
            .filter(effective_date__gte=date_start, effective_date__lt=date_end)
            .select_related("property")
            .order_by("-effective_date", "-created_at")
        )
        pq_local = (property_query or "").strip()
        if pq_local:
            prop_ids = list(
                Property.objects.filter(landlord=landlord)
                .filter(Q(name__icontains=pq_local) | Q(address__icontains=pq_local))
                .values_list("pk", flat=True)[:20]
            )
            q = q.filter(property_id__in=prop_ids) if prop_ids else q.none()
        return q

    qs = _base_qs(start, end)
    pq = (property_query or "").strip()

    amt_s = (amount or "").strip().replace("$", "").replace(",", "")
    if amt_s:
        try:
            from decimal import Decimal as D

            target = D(amt_s)
            qs_list = list(qs[: max(1, min(limit, 100))])
            exact = [e for e in qs_list if e.amount == target]
            if exact:
                rows = [_serialize_expense(e) for e in exact]
            else:
                rows = [_serialize_expense(e) for e in qs_list]
                total = sum((D(r["amount"]) for r in rows), D("0"))
                return {
                    "as_of": today.isoformat(),
                    "month_window": {
                        "from": start.isoformat(),
                        "to": end.isoformat(),
                    },
                    "query": {
                        "month": month or "",
                        "day": day_s,
                        "property_query": pq,
                        "amount": amt_s,
                    },
                    "count": len(rows),
                    "total": str(total),
                    "amount_match": str(total) == str(target),
                    "note": (
                        f"No single expense equals {target}; total of listed lines is {total}. "
                        f"If that matches the user's figure, report the line items."
                    ),
                    "expenses": rows,
                }
            total = sum((D(r["amount"]) for r in rows), D("0"))
            return {
                "as_of": today.isoformat(),
                "month_window": {"from": start.isoformat(), "to": end.isoformat()},
                "query": {
                    "month": month or "",
                    "day": day_s,
                    "property_query": pq,
                    "amount": amt_s,
                },
                "count": len(rows),
                "total": str(total),
                "expenses": rows,
            }
        except Exception:  # noqa: BLE001
            pass

    rows = [_serialize_expense(e) for e in qs[: max(1, min(limit, 100))]]
    total = sum((Decimal(r["amount"]) for r in rows), Decimal("0.00"))
    not_yet = sum(
        (
            Decimal(r["amount"])
            for r in rows
            if r["bank_status"] == "NOT_YET_TAKEN"
        ),
        Decimal("0.00"),
    )

    # If user asked about a specific day (often "today") and got nothing,
    # attach this calendar month's expenses so the answer can still be useful.
    this_month_expenses: list[dict] = []
    this_month_total = Decimal("0.00")
    empty_day_note = ""
    if day_filter is not None and not rows:
        m_start, m_end = _month_bounds(day_filter)
        this_month_expenses = [
            _serialize_expense(e)
            for e in _base_qs(m_start, m_end)[: max(1, min(limit, 100))]
        ]
        this_month_total = sum(
            (Decimal(r["amount"]) for r in this_month_expenses), Decimal("0.00")
        )
        empty_day_note = (
            f"No expenses with effective_date = {day_filter.isoformat()}. "
            f"Utility/bills often use period end dates (e.g. 2026-07-21, 2026-07-31), "
            f"not 'today'. This calendar month ({m_start.strftime('%Y-%m')}) has "
            f"{len(this_month_expenses)} expense(s) totaling ${this_month_total}. "
            f"When the user asks 'expenses today' casually, report this month's "
            f"open expenses and say none posted for that exact day."
        )

    return {
        "as_of": today.isoformat(),
        "month_window": {"from": start.isoformat(), "to": end.isoformat()},
        "query": {
            "month": month or (start.strftime("%Y-%m") if not day_filter else ""),
            "day": day_s,
            "property_query": pq,
        },
        "count": len(rows),
        "total": str(total),
        "not_yet_taken_from_bank": str(not_yet),
        "expenses": rows,
        "this_month_expenses": this_month_expenses,
        "this_month_total": str(this_month_total) if this_month_expenses else None,
        "empty_day_note": empty_day_note or None,
        "rules": {
            "year": (
                f"as_of is {today.isoformat()}. Current calendar month is "
                f"{today.strftime('%Y-%m')}. Never report a different year unless "
                f"the user asked for it or the expense effective_date shows it."
            ),
            "today_vs_month": (
                "effective_date is the service/ledger date, not necessarily when "
                "the landlord asked. 'Expenses today' with count=0 should still "
                "mention this_month_expenses if present."
            ),
            "bank": (
                "bank_status NOT_YET_TAKEN means recorded in the ledger but money "
                "has not left the bank yet (matches the Financial page banner)."
            ),
        },
    }


def _lease_covers_day(lease, day: date) -> bool:
    """True if [start_date, end_date] includes day (open-ended end = forever)."""
    if lease.start_date > day:
        return False
    if lease.end_date is not None and lease.end_date < day:
        return False
    return True


def _occupancy_phase(lease, today: date) -> str:
    """Lease-derived occupancy — never use listing marketing status for this.

    - occupied_now: active term covers today (tenant in / term has started)
    - leased_future: signed/active (or pending signatures) but start is still
      ahead — room is rented for those dates even if still empty today and
      even if the listing is still marked Available for advertising
    - pending_signatures: not yet fully signed; still a commitment in progress
    """
    from rentium.leases.models import Lease

    if lease.status == Lease.LeaseStatus.PENDING_SIGNATURES:
        if lease.start_date > today:
            return "leased_future_pending_signatures"
        return "pending_signatures"
    if lease.start_date > today:
        return "leased_future"
    return "occupied_now"


def _tenant_rows_for_lease(lease) -> list[dict]:
    rows = []
    for lt in lease.lease_tenants.all():
        email = lt.invited_email or (
            lt.tenant.user.email if lt.tenant_id else ""
        )
        if lt.has_signed:
            invite_status = "signed"
        elif lt.tenant_id:
            invite_status = "linked_unsigned"
        elif lt.invite_sent_at:
            invite_status = "invite_sent_awaiting_signature"
        elif email:
            invite_status = "slot_created_invite_not_sent"
        else:
            invite_status = "empty_slot"
        rows.append(
            {
                "name": lt.display_name,
                "email": email,
                "is_primary": lt.is_primary_tenant,
                "has_signed": lt.has_signed,
                "invite_status": invite_status,
                "invite_sent_at": lt.invite_sent_at.isoformat()
                if lt.invite_sent_at
                else None,
                "declined": bool(getattr(lt, "declined", False)),
            }
        )
    if not rows:
        rows = []  # empty = no one invited yet
    return rows


def _lease_overlaps_month(lease, month_start: date, month_end_exclusive: date) -> bool:
    """True if the lease term overlaps the calendar month [start, end)."""
    if lease.start_date >= month_end_exclusive:
        return False
    if lease.end_date is not None and lease.end_date < month_start:
        return False
    return True


def _serialize_lease_brief(lease, today: date, *, place_name: str = "") -> dict:
    phase = _occupancy_phase(lease, today)
    end = lease.end_date.isoformat() if lease.end_date else None
    covers_today = _lease_covers_day(lease, today)
    month_start, month_end = _month_bounds(today)
    overlaps_month = _lease_overlaps_month(lease, month_start, month_end)
    # "Has a commitment" ≠ "occupied today" ≠ "earning rent this calendar month"
    has_commitment = phase.startswith("leased") or phase in (
        "occupied_now",
        "pending_signatures",
    )
    # Agreement form (what landlords mean by "lease type") vs term shape.
    agreement_code = lease.lease_type or ""
    try:
        agreement_label = lease.get_lease_type_display()
    except Exception:  # noqa: BLE001 — defensive for odd legacy values
        agreement_label = agreement_code
    term_shape = "month_to_month" if lease.is_month_to_month else "fixed_term"
    return {
        "lease_id": str(lease.pk),
        "lease_number": lease.lease_number or "",
        "status": lease.status,
        "occupancy_phase": phase,
        "property": place_name
        or (
            lease.property.name
            if lease.property_id
            else (lease.group.name if lease.group_id else "")
        ),
        # Primary answer to "what type of lease / agreement is this?"
        "agreement_type": agreement_label,
        "agreement_type_code": agreement_code,
        "lease_type": agreement_code,
        "lease_type_display": agreement_label,
        # Secondary: fixed term vs month-to-month (dates), NOT the agreement form.
        "term_shape": term_shape,
        "is_month_to_month": lease.is_month_to_month,
        "start_date": lease.start_date.isoformat(),
        "end_date": end,
        "move_out_date": lease.move_out_date.isoformat()
        if lease.move_out_date
        else end,
        "monthly_rent": str(lease.get_total_monthly_rent()),
        "tenants": _tenant_rows_for_lease(lease),
        "covers_today": covers_today,
        "vacant_today": not covers_today,
        "occupied_today": covers_today and phase == "occupied_now",
        "has_future_commitment": phase
        in ("leased_future", "leased_future_pending_signatures"),
        "has_lease_commitment": has_commitment,
        # Back-compat: "rented" meant commitment — keep but prefer the fields above.
        "rented": has_commitment,
        "term_overlaps_this_calendar_month": overlaps_month,
        "summary": _lease_occupancy_summary(lease, phase, today),
        "type_hint": (
            f"Agreement: {agreement_label}. Term shape: {term_shape} "
            f"({lease.start_date.isoformat()} → {end or 'open'}). "
            f"When asked 'lease type', lead with the agreement name, then term shape."
        ),
    }


def _lease_occupancy_summary(lease, phase: str, today: date | None = None) -> str:
    today = today or date.today()
    start = lease.start_date.isoformat()
    end = lease.end_date.isoformat() if lease.end_date else "open-ended"
    as_of = today.isoformat()
    if phase == "occupied_now":
        return (
            f"Occupied today ({as_of}). Term {start} → {end}. "
            f"Listing may still say Available if re-advertising."
        )
    if phase == "leased_future":
        return (
            f"Vacant today ({as_of}) — term has not started. "
            f"Signed/active lease starts {start} ends {end}. "
            f"Say: vacant until {start}, then rented. "
            f"Do NOT say occupied/rented today or this month if the term "
            f"starts next month. Do NOT say 'no lease' — there is a commitment."
        )
    if phase == "leased_future_pending_signatures":
        return (
            f"Vacant today ({as_of}). Lease pending signatures for {start} → {end}. "
            f"Not occupied until the term starts and is signed."
        )
    if phase == "pending_signatures":
        return f"Lease pending signatures (term {start} → {end}). As of {as_of}."
    return f"Lease {lease.status} ({start} → {end}). As of {as_of}."


def _active_leases_by_property(landlord, property_ids: list, today: date) -> dict:
    """Map property_id -> best relevant lease (covers today, else soonest future)."""
    from django.db.models import Q

    from rentium.leases.models import Lease, LeaseTenant

    if not property_ids:
        return {}

    id_set = set(property_ids)
    statuses = (
        Lease.LeaseStatus.ACTIVE,
        Lease.LeaseStatus.PENDING_SIGNATURES,
    )
    leases = (
        Lease.objects.filter(landlord=landlord, status__in=statuses)
        .filter(
            Q(property_id__in=property_ids)
            | Q(lease_tenants__room_id__in=property_ids)
        )
        .distinct()
        .select_related("property", "group")
        .prefetch_related("lease_tenants__tenant__user")
        .order_by("start_date")
    )

    # property_id -> list of leases
    by_prop: dict = {pid: [] for pid in property_ids}
    for lease in leases:
        if lease.property_id and lease.property_id in id_set:
            by_prop[lease.property_id].append(lease)
        # Group / multi-room: attach via tenant room assignment
        for lt in lease.lease_tenants.all():
            room_id = getattr(lt, "room_id", None)
            if room_id and room_id in id_set:
                if lease not in by_prop[room_id]:
                    by_prop[room_id].append(lease)

    chosen: dict = {}
    for pid, group in by_prop.items():
        if not group:
            continue
        covering = [L for L in group if _lease_covers_day(L, today)]
        if covering:
            # Prefer ACTIVE over PENDING if both cover
            covering.sort(
                key=lambda L: (0 if L.status == Lease.LeaseStatus.ACTIVE else 1)
            )
            chosen[pid] = covering[0]
            continue
        future = [L for L in group if L.start_date > today]
        if future:
            future.sort(key=lambda L: L.start_date)
            chosen[pid] = future[0]
            continue
        # Fallback: any remaining (shouldn't happen often)
        chosen[pid] = group[0]
    return chosen


def suggested_lease_for_property(prop) -> dict:
    """What agreement Rentium offers for a *new* lease on this listing.

    Mirrors leases.api.views.lease_types_view: rooms → Standard Roommate;
    complete units → province-specific residential (BC RTB-1, etc.).
    """
    from rentium.properties.models import Property

    cat = prop.property_category
    prov = (prop.province or "").upper()
    if cat == Property.PropertyCategory.ROOM:
        return {
            "agreement_type_code": "GENERIC_ROOMMATE",
            "agreement_type": "Standard Roommate Agreement",
            "property_category": cat,
            "province": prov or None,
            "reason": (
                "Room listings use the Standard Roommate Agreement for new leases "
                "(province-agnostic)."
            ),
            "also_known_as": ["roommate agreement", "room lease", "standard room lease"],
        }
    # Complete unit
    if prov == "BC":
        return {
            "agreement_type_code": "BC_RESIDENTIAL",
            "agreement_type": "BC Residential Tenancy (RTB-1)",
            "property_category": cat,
            "province": "BC",
            "reason": (
                "Complete units in British Columbia use the BC Residential Tenancy "
                "agreement (RTB-1) for new leases."
            ),
            "also_known_as": [
                "RTB lease",
                "RTB-1",
                "residential tenancy",
                "BC residential lease",
            ],
        }
    if prov == "SK":
        return {
            "agreement_type_code": "SK_RESIDENTIAL",
            "agreement_type": "Saskatchewan Residential Tenancy",
            "property_category": cat,
            "province": "SK",
            "reason": "Complete units in Saskatchewan use the SK residential agreement.",
            "also_known_as": ["residential tenancy", "SK residential lease"],
        }
    return {
        "agreement_type_code": "GENERIC_RESIDENTIAL",
        "agreement_type": "Standard Residential Agreement",
        "property_category": cat,
        "province": prov or None,
        "reason": (
            "Complete unit outside BC/SK (or province unset) uses the generic "
            "residential agreement for new leases."
        ),
        "also_known_as": ["residential lease", "standard residential"],
    }


def _property_type_payload(prop) -> dict:
    """Human-facing type fields — never leave the model to invent 'Condo'."""
    from rentium.properties.models import Property

    cat = prop.property_category
    try:
        cat_display = prop.get_property_category_display()
    except Exception:  # noqa: BLE001
        cat_display = cat
    unit_type = prop.unit_type or None
    unit_display = None
    if unit_type:
        try:
            unit_display = prop.get_unit_type_display()
        except Exception:  # noqa: BLE001
            unit_display = unit_type
    room_type = getattr(prop, "room_type", None) or None
    room_display = None
    if room_type:
        try:
            room_display = prop.get_room_type_display()
        except Exception:  # noqa: BLE001
            room_display = room_type

    if cat == Property.PropertyCategory.ROOM:
        kind_summary = f"Room · {room_display or 'Private/shared room'}"
        primary_type = room_display or "Room"
    else:
        kind_summary = f"Complete unit · {unit_display or 'Unit'}"
        primary_type = unit_display or "Complete unit"

    province = prop.province or ""
    if province:
        kind_summary = f"{kind_summary} ({province})"

    suggested = suggested_lease_for_property(prop)
    return {
        "category": cat,
        "category_display": cat_display,
        "unit_type": unit_type,
        "unit_type_display": unit_display,
        "room_type": room_type,
        "room_type_display": room_display,
        "province": province or None,
        # What to say for "what type of property is it?"
        "primary_type": primary_type,
        "kind_summary": kind_summary,
        "type_hint": (
            f"Name: {prop.name}. Type: {kind_summary}. "
            f"Answer 'what type of property' with unit_type_display/room_type_display "
            f"({primary_type}), not a guess like Condo. "
            f"If they ask what lease it would use when created: "
            f"{suggested['agreement_type']}."
        ),
        "suggested_lease_if_created": suggested,
    }


def live_context(landlord) -> dict:
    """Compact authoritative portfolio card injected into every RAMA turn.

    Models frequently skip tools or invent numbers; this is the non-optional
    ground truth for the current landlord as of today.
    """
    snap = state_of_the_union(landlord)
    listings_brief = []
    inv = snap.get("listings") or {}
    for row in (inv.get("rooms") or []) + (inv.get("complete_units") or []):
        occ = row.get("occupancy") or {}
        lease = occ.get("lease") or {}
        listings_brief.append(
            {
                "name": row.get("name"),
                "address": row.get("address"),
                "holding": row.get("holding"),
                "primary_type": row.get("primary_type"),
                "group": row.get("group"),
                "category": row.get("category"),
                "layout": row.get("layout"),
                "listing_status": row.get("listing_status") or row.get("status"),
                "has_images": row.get("has_images"),
                "image_count": row.get("image_count"),
                "vacant_today": occ.get("vacant_today"),
                "occupied_today": occ.get("occupied_today"),
                "phase": occ.get("phase"),
                "has_lease_commitment": occ.get("has_lease_commitment"),
                "lease_number": lease.get("lease_number") or None,
                "agreement_type": lease.get("agreement_type") or None,
                "monthly_rent": lease.get("monthly_rent") or None,
                "lease_start": lease.get("start_date") or None,
                "lease_end": lease.get("end_date") or None,
                "tenants": lease.get("tenants") or [],
                "suggested_lease_if_created": row.get("suggested_lease_if_created"),
            }
        )
    # Draft leases are easy to miss when only LIVE PORTFOLIO is used.
    draft_leases = []
    try:
        all_leases = list_leases(landlord, include_drafts=True, include_ended=False)
        for row in all_leases.get("leases") or []:
            if row.get("is_draft") or (row.get("status") or "").upper() == "DRAFT":
                draft_leases.append(
                    {
                        "property": row.get("property") or row.get("property_name"),
                        "lease_number": row.get("lease_number"),
                        "status": row.get("status"),
                        "agreement_type": row.get("agreement_type"),
                        "start_date": row.get("start_date"),
                        "end_date": row.get("end_date"),
                        "summary": row.get("summary"),
                        "rented": False,
                    }
                )
    except Exception:
        draft_leases = []

    outstanding = snap.get("outstanding") or {
        "total": "0.00",
        "count": 0,
        "overdue_count": 0,
    }

    return {
        "as_of": snap.get("as_of"),
        "dashboard_truth": snap.get("dashboard_truth"),
        "layout": snap.get("layout"),
        "property_structure": {
            "counts": inv.get("counts") or {},
            "holdings": inv.get("holdings") or [],
            "unassigned_listings": inv.get("unassigned_listings") or [],
            "rule": (
                "A holding is a physical house/building. A listing is a rentable "
                "room or complete unit inside it. Never use the words interchangeably."
            ),
        },
        "listings": listings_brief,
        "rented_or_committed_listings": snap.get("rented_listings") or [],
        "draft_leases": draft_leases,
        "draft_lease_count": len(draft_leases),
        "this_month_expenses": snap.get("this_month_expenses") or [],
        "this_month_money": {
            k: (snap.get("this_month") or {}).get(k)
            for k in (
                "month",
                "label",
                "expected_income",
                "collected_income",
                "expenses",
                "expenses_not_yet_taken_from_bank",
                "net",
                "deposits_collected",
            )
        },
        "deposits_held": snap.get("deposits_held"),
        "outstanding": outstanding,
        "outstanding_total": outstanding.get("total") or "0.00",
        "next_charge": snap.get("next_charge"),
        "upcoming_appointments": snap.get("upcoming_appointments") or [],
        "attention": snap.get("attention"),
        "open_work_orders": snap.get("open_work_orders"),
        "domain_digest": (digest := _safe_domain_digest(landlord)),
        "inventory_brief": _inventory_brief(landlord),
        "inventory_hint": (
            f"{digest.get('inventory_items_private', 0)} private + "
            f"{digest.get('inventory_items_shared', 0)} shared inventory items on file. "
            "If >0 there IS inventory — list from inventory_brief or list_inventory. "
            "Never say 'no furniture' / 'none recorded' when counts > 0."
            if not digest.get("error")
            else ""
        ),
        "instructions": (
            "These figures are live from the database for THIS landlord. "
            "They override any earlier message in the chat that disagrees. "
            "has_images / image_count are authoritative — never guess or infer "
            "whether a listing has photos from anything else. "
            "Room E with a lease_number is an active commitment even if vacant_today. "
            "Expense lines must be quoted by description+amount from this_month_expenses only. "
            "draft_leases / draft_lease_count: DRAFT paperwork only — not rented; "
            "still answer 'any draft leases?' from this list. "
            "outstanding_total is money OWED now (due_date <= today, unpaid). "
            "next_charge is FUTURE and is NOT outstanding. "
            "If vacant_today is true, answer 'Is X vacant today?' with Yes first. "
            "For viewings use date+weekday from upcoming_appointments only — "
            "never invent 'tomorrow' or wrong weekdays. "
            "Layout: rooms listed together under layout.groups[].listings "
            "(e.g. Room D and Room E under McKenzie Side Unit) ARE the same "
            "household unit → Yes. Garden Suite in standalone_units is a "
            "different unit → No vs D/E. "
            "Property type questions: answer primary_type (Garden Suite / "
            "Private Room), not only category COMPLETE_UNIT. "
            "For follow-up layout questions, use each listing's layout object. "
            "Null means not recorded; never infer it from the name or description. "
            "property_structure distinguishes physical holdings from rental listings. "
            "Expenses: quote description + amount + property; do not override "
            "a clear description (Telus) with a mismatched vendor field. "
            "domain_digest has counts for work orders, inquiries, messages, "
            "inspections, move-ins, inventory — zero means none (say none). "
            "If inventory_hint says items on file, call list_inventory; "
            "never answer 'no furniture' when private/shared counts > 0. "
            "Use list_work_orders / list_inquiries / list_conversations / "
            "list_inspections / list_move_events / list_inventory / "
            "charge_schedule / list_tenants / list_documents for detail."
        ),
    }


def _safe_domain_digest(landlord) -> dict:
    try:
        from .domain_reads import domain_digest

        return domain_digest(landlord)
    except Exception as exc:  # noqa: BLE001
        return {"error": str(exc)}


def _inventory_brief(landlord) -> dict:
    """Top inventory lines for LIVE PORTFOLIO so 'any furniture?' is accurate."""
    try:
        from .domain_reads import list_inventory

        inv = list_inventory(landlord, limit=40)
        private = [
            {
                "property": r.get("property"),
                "name": r.get("name"),
                "qty": r.get("quantity"),
                "condition": r.get("condition_display") or r.get("condition"),
            }
            for r in (inv.get("private_inventory") or [])[:20]
        ]
        shared = [
            {
                "group": r.get("group"),
                "name": r.get("name"),
                "qty": r.get("quantity"),
            }
            for r in (inv.get("shared_inventory") or [])[:10]
        ]
        return {
            "counts": inv.get("counts"),
            "private_sample": private,
            "shared_sample": shared,
            "rule": (
                "If counts.total > 0, answer yes and list names. "
                "Call list_inventory for full detail."
            ),
        }
    except Exception as exc:  # noqa: BLE001
        return {"error": str(exc)}


def property_layout(landlord) -> dict:
    """How listings relate: shared room groups vs standalone complete units.

    Same street address does NOT mean same unit. Room groups share common
    spaces; complete units (e.g. garden suite) are separate even if co-addressed.
    """
    from rentium.properties.models import Property, PropertyGroup

    groups = []
    grouped_ids: set = set()
    for g in PropertyGroup.objects.filter(landlord=landlord).prefetch_related(
        "grouped_properties"
    ):
        members = list(g.grouped_properties.all())
        if not members:
            continue
        for m in members:
            grouped_ids.add(m.pk)
        addrs = sorted({(m.address or "").strip() for m in members if m.address})
        groups.append(
            {
                "group_name": g.name,
                "kind": "shared_room_group",
                "listings": [m.name for m in members],
                "listing_ids": [str(m.pk) for m in members],
                "addresses": addrs,
                "note": (
                    f"'{g.name}' is one household unit: these rooms share common "
                    f"spaces (kitchen, bath, etc.). They are NOT separate full units."
                ),
            }
        )

    standalone = []
    for prop in (
        Property.objects.filter(landlord=landlord, is_active_offering=True)
        .exclude(pk__in=grouped_ids)
        .order_by("name")
    ):
        type_payload = _property_type_payload(prop)
        standalone.append(
            {
                "name": prop.name,
                "kind": (
                    "complete_unit"
                    if prop.property_category
                    == Property.PropertyCategory.COMPLETE_UNIT
                    else "room_without_group"
                ),
                "primary_type": type_payload["primary_type"],
                "category": prop.property_category,
                "address": prop.address,
                "note": (
                    f"'{prop.name}' is a separate listing"
                    + (
                        " (self-contained complete unit — not the same unit as "
                        "rooms that only share this street address)."
                        if prop.property_category
                        == Property.PropertyCategory.COMPLETE_UNIT
                        else "."
                    )
                ),
            }
        )

    total = Property.objects.filter(
        landlord=landlord, is_active_offering=True
    ).count()
    return {
        "total_listings": total,
        "count_rule": (
            f"There are {total} rentable listings. Do not call each listing a "
            "physical property: use property_inventory counts.physical_containers "
            "for houses/buildings and explain both counts when 'properties' is ambiguous."
        ),
        "same_address_rule": (
            "Same street address can hold multiple independent units "
            "(e.g. main-house rooms in a group + a detached garden suite). "
            "Use groups[] and standalone_units[] — never say all co-addressed "
            "listings are one unit."
        ),
        "groups": groups,
        "standalone_units": standalone,
    }


def property_inventory(landlord, *, limit: int = 100) -> dict:
    """List physical holdings and every rentable listing within them.

    Each listing includes marketing ``listing_status`` (Available/Occupied/…),
    full type fields (Garden Suite / Room / …), suggested lease if none yet,
    AND lease-derived occupancy.
    """
    from rentium.properties.models import Property

    today = date.today()
    # Listings parked by a rental-mode switch are history, not inventory. They
    # are kept so a switch back reuses them, but counting them here is how
    # "what do I have?" came back as 21 listings for a 12-listing portfolio —
    # and, worse, as 14 rooms for a portfolio with 5.
    parked_total = Property.objects.filter(
        landlord=landlord, is_active_offering=False
    ).count()
    qs = list(
        Property.objects.filter(landlord=landlord, is_active_offering=True)
        .select_related("group", "holding", "unit", "unit__holding")
        .prefetch_related("primary_area_associations")
        # Grounds has_images/image_count without an N+1; Property helpers
        # (gallery_image_count etc.) honour this annotation.
        .annotate(_gallery_count=Count("property_images", distinct=True))
        .order_by("name")[: max(1, min(limit, 200))]
    )
    prop_ids = [p.pk for p in qs]
    lease_map = _active_leases_by_property(landlord, prop_ids, today)

    rooms: list[dict] = []
    units: list[dict] = []
    status_counts: dict[str, int] = {}
    occ_counts = {
        "occupied_today": 0,
        "vacant_today": 0,
        "occupied_now": 0,  # alias of occupied_today
        "leased_future": 0,  # vacant today but has future term commitment
        "pending_signatures": 0,
        "truly_unleased": 0,  # no active/pending lease at all
        "vacant": 0,  # alias: truly_unleased (legacy name — prefer vacant_today)
        "has_lease_commitment": 0,  # occupied_today + leased_future + pending
        "rented_or_committed": 0,  # same as has_lease_commitment (legacy)
        "term_overlaps_this_calendar_month": 0,
    }

    for prop in qs:
        status_counts[prop.status] = status_counts.get(prop.status, 0) + 1
        lease = lease_map.get(prop.pk)
        if lease:
            brief = _serialize_lease_brief(lease, today, place_name=prop.name)
            phase = brief["occupancy_phase"]
            vacant_today = brief["vacant_today"]
            if vacant_today:
                occ_counts["vacant_today"] += 1
            else:
                occ_counts["occupied_today"] += 1
                occ_counts["occupied_now"] += 1
            if phase in ("leased_future", "leased_future_pending_signatures"):
                occ_counts["leased_future"] += 1
            elif phase == "pending_signatures":
                occ_counts["pending_signatures"] += 1
            occ_counts["has_lease_commitment"] += 1
            occ_counts["rented_or_committed"] += 1
            if brief["term_overlaps_this_calendar_month"]:
                occ_counts["term_overlaps_this_calendar_month"] += 1
            occupancy = {
                "phase": phase,
                "vacant_today": vacant_today,
                "occupied_today": not vacant_today,
                "is_rented_or_committed": True,
                "has_lease_commitment": True,
                "has_future_commitment": brief["has_future_commitment"],
                "term_overlaps_this_calendar_month": brief[
                    "term_overlaps_this_calendar_month"
                ],
                # True only if empty today (future lease or pending) — can still show.
                "is_vacant_for_new_tenant_now": vacant_today,
                "listing_status_means_marketing_only": True,
                "lease": brief,
                "explanation": brief["summary"],
                "answer_hints": {
                    "which_are_rented_today": (
                        "occupied" if not vacant_today else "vacant today"
                    ),
                    "which_have_leases": "has commitment",
                    "rented_this_month": (
                        "yes — term overlaps this calendar month"
                        if brief["term_overlaps_this_calendar_month"]
                        else "no — term does not cover this calendar month"
                    ),
                },
            }
        else:
            occ_counts["vacant_today"] += 1
            occ_counts["truly_unleased"] += 1
            occ_counts["vacant"] += 1
            occupancy = {
                "phase": "vacant",
                "vacant_today": True,
                "occupied_today": False,
                "is_rented_or_committed": False,
                "has_lease_commitment": False,
                "has_future_commitment": False,
                "term_overlaps_this_calendar_month": False,
                "is_vacant_for_new_tenant_now": prop.status
                == Property.PropertyStatus.AVAILABLE,
                "listing_status_means_marketing_only": True,
                "lease": None,
                "explanation": (
                    f"Vacant today ({today.isoformat()}) with no active or pending "
                    f"lease — truly unleased. Marketing status: {prop.status}."
                ),
                "answer_hints": {
                    "which_are_rented_today": "vacant today",
                    "which_have_leases": "no lease",
                    "rented_this_month": "no",
                },
            }

        type_payload = _property_type_payload(prop)
        internal_areas = [
            {
                "type": area.area_type,
                "type_display": area.get_area_type_display(),
                "count": area.count,
                "description": area.description or None,
            }
            # Seeded placeholders are excluded on purpose: they are
            # scaffolding for maintenance/inspections, not layout the landlord
            # told us about. Reporting them would turn "unknown" into invented
            # fact.
            for area in prop.primary_area_associations.filter(
                is_seeded_default=False
            )
        ]
        row = {
            "id": str(prop.pk),
            "name": prop.name,
            # Renamed in payload meaning: this is the marketing/ops field only.
            "listing_status": prop.status,
            "status": prop.status,  # backwards compatible alias
            "address": prop.address,
            "city": prop.city,
            "group": prop.group.name if prop.group_id else None,
            "holding": prop.holding.name if prop.holding_id else None,
            # The physical space this listing offers. Several listings on one
            # unit are ONE place being rented, not several properties.
            "unit": prop.unit.name if prop.unit_id else None,
            "rental_mode": prop.unit.rental_mode if prop.unit_id else None,
            "occupancy": occupancy,
            # Photos — computed in Python, never for the model to infer.
            "has_primary_image": bool(prop.primary_image),
            "image_count": prop.image_count,
            "has_images": prop.image_count > 0,
            "publish_blockers": prop.publish_blockers(),
            "layout": {
                "bedrooms": prop.bedrooms,
                "bathrooms": (
                    str(prop.bathrooms) if prop.bathrooms is not None else None
                ),
                "max_occupancy": prop.max_occupancy,
                "square_footage": prop.square_footage,
                "internal_areas": internal_areas,
                "recorded_internal_area_count": sum(
                    area["count"] for area in internal_areas
                ),
                "room_count_guidance": (
                    "For a complete unit, 'rooms' can mean bedrooms or all internal "
                    "spaces. Report both recorded values; if absent, say unknown and "
                    "ask which count the landlord means. Never infer or edit."
                ),
            },
            **type_payload,
        }
        if prop.property_category == Property.PropertyCategory.ROOM:
            rooms.append(row)
        else:
            units.append(row)

    # Live offerings only — parked listings are reported separately as
    # parked_listings so they can be mentioned when asked about, and never
    # counted as things currently on the market.
    live = Property.objects.filter(landlord=landlord, is_active_offering=True)
    total = live.count()
    room_total = live.filter(
        property_category=Property.PropertyCategory.ROOM
    ).count()
    unit_total = live.filter(
        property_category=Property.PropertyCategory.COMPLETE_UNIT,
    ).count()

    layout = property_layout(landlord)
    holdings = []
    assigned_ids: set = set()
    for holding in landlord.property_holdings.prefetch_related("listings").all():
        members = [p for p in qs if p.holding_id == holding.pk]
        assigned_ids.update(p.pk for p in members)
        holdings.append(
            {
                "id": str(holding.pk),
                "name": holding.name,
                "kind": holding.kind,
                "address": holding.address,
                "city": holding.city,
                "listing_count": len(members),
                "listings": [p.name for p in members],
            }
        )
    unassigned = [p for p in qs if p.pk not in assigned_ids]
    unassigned_addresses = {
        ((p.address or "").strip().casefold(), (p.city or "").strip().casefold())
        for p in unassigned
        if (p.address or "").strip()
    }
    physical_container_count = len(holdings) + len(unassigned_addresses)
    # The physical layer: what actually exists, independent of how many
    # listings currently sit on it.
    from rentium.properties.models import PropertyUnit

    unit_rows = []
    for u in (
        PropertyUnit.objects.filter(landlord=landlord)
        .select_related("holding")
        .prefetch_related("offerings", "areas")
        .order_by("holding__name", "name")
    ):
        recorded = u.areas.filter(is_seeded_default=False)
        beds = recorded.filter(area_type="BEDROOM").count()
        baths = recorded.filter(area_type="BATHROOM").count()
        unit_rows.append(
            {
                "name": u.name,
                "holding": u.holding.name,
                "rented": u.rental_mode,
                # null = never recorded. NOT zero, and never a reason to write.
                "bedrooms": beds or None,
                "bathrooms": baths or None,
                "layout_complete": u.layout_complete,
                "not_recorded": u.missing_layout_notes or None,
                "listings": [
                    o.name for o in u.offerings.all() if o.is_active_offering
                ],
            }
        )

    room_hierarchy: list[dict] = []
    hierarchy_index: dict[tuple[str, str, str], dict] = {}
    for room in rooms:
        key = (
            room.get("holding") or "",
            room.get("address") or "",
            room.get("group") or "",
        )
        bucket = hierarchy_index.get(key)
        if bucket is None:
            bucket = {
                "holding": room.get("holding"),
                "address": room.get("address"),
                "city": room.get("city"),
                "property_group": room.get("group"),
                "rooms": [],
            }
            hierarchy_index[key] = bucket
            room_hierarchy.append(bucket)
        bucket["rooms"].append(
            {
                "id": room["id"],
                "name": room["name"],
                "listing_status": room["listing_status"],
                "occupancy": room["occupancy"],
            }
        )
    return {
        "as_of": today.isoformat(),
        "layout": layout,
        "rules": {
            "time_aware_occupancy": (
                f"as_of is {today.isoformat()}. "
                "vacant_today / occupied_today answer 'is anyone living there TODAY?'. "
                "phase leased_future means vacant TODAY but a lease starts later — "
                "say 'vacant until START, then rented', never 'rented today'. "
                "term_overlaps_this_calendar_month answers 'rented out this month'. "
                "has_lease_commitment answers 'is there a signed/pending lease at all?'."
            ),
            "listing_status_vs_occupancy": (
                "listing_status (Available/Occupied/…) is marketing only. "
                "Never use it alone to decide vacant vs rented."
            ),
            "question_routing": (
                "'how many properties?' is ambiguous: answer with "
                "counts.physical_units (the floors/suites that physically exist) "
                "and counts.total_listings (what is on the market), clearly named. "
                "counts.parked_listings are NOT on the market — mention them only "
                "if asked about past arrangements. "
                "'are rooms in one unit?' → layout.groups vs layout.standalone_units. "
                "'which rooms are rented / occupied now?' → occupancy.occupied_today / vacant_today. "
                "'which have leases / are committed?' → has_lease_commitment / phase leased_future. "
                "'rented this month / next month?' → occupancy_as_of tool or lease dates. "
                "'what type of property?' → primary_type / unit_type_display / kind_summary. "
                "'how many rooms does a complete unit have?' → its layout bedrooms "
                "and internal_areas; null means not recorded, never a reason to write. "
                "'what lease would it have?' → suggested_lease_if_created.agreement_type."
            ),
            "no_invention": (
                "Never invent expense descriptions, rent totals, or deposit totals. "
                "Copy amounts from expense_lines / dashboard_truth / this_month exactly."
            ),
        },
        "units": unit_rows,
        "unit_display_rule": (
            "A UNIT is one physical floor/suite. A unit rented whole has ONE "
            "listing; a unit rented by the room has one per bedroom. Answer "
            "'how many places do I have?' with counts.physical_units, and never "
            "present the bedrooms of a whole-unit floor as separate properties."
        ),
        "counts": {
            "physical_holdings": len(holdings),
            "physical_units": len(unit_rows),
            "physical_containers": physical_container_count,
            "unassigned_address_containers": len(unassigned_addresses),
            "total_listings": total,
            "parked_listings": parked_total,
            "rooms": room_total,
            "complete_units": unit_total,
            "by_listing_status": status_counts,
            "by_status": status_counts,  # alias
            "by_occupancy": occ_counts,
        },
        "rooms": rooms,
        "room_hierarchy": room_hierarchy,
        "room_display_rule": (
            "When showing all rooms, group room_hierarchy by physical holding/"
            "address, then property_group. Do not present each room as a separate "
            "physical property."
        ),
        "complete_units": units,
        "holdings": holdings,
        "unassigned_listings": [p.name for p in unassigned],
        "truncated": total > len(rooms) + len(units),
    }


def occupancy_as_of(landlord, on_date: str = "") -> dict:
    """Occupancy of every listing on a given calendar day (YYYY-MM-DD).

    Use for "next month", "in August", "on 2026-08-15". Empty on_date = today.
    """
    today = date.today()
    if on_date:
        try:
            day = date.fromisoformat(on_date.strip())
        except ValueError:
            return {"error": f"on_date must be YYYY-MM-DD, got {on_date!r}"}
    else:
        day = today

    from rentium.properties.models import Property

    # Parked listings would otherwise report as vacant and inflate the
    # vacancy picture with places that aren't being offered.
    props = list(
        Property.objects.filter(landlord=landlord, is_active_offering=True)
        .select_related("group")
        .order_by("name")
    )
    lease_map = _active_leases_by_property(landlord, [p.pk for p in props], day)
    rows = []
    occupied = 0
    vacant = 0
    for prop in props:
        lease = lease_map.get(prop.pk)
        if lease and _lease_covers_day(lease, day):
            phase = "occupied_on_date"
            occupied += 1
            vacant_flag = False
            brief = _serialize_lease_brief(lease, day, place_name=prop.name)
            # Force date-relative flags for the asked day
            brief = {
                **brief,
                "covers_today": True,
                "vacant_today": False,
                "occupied_today": True,
                "summary": (
                    f"Occupied on {day.isoformat()} under lease "
                    f"{brief.get('lease_number') or brief['lease_id']} "
                    f"({brief['start_date']} → {brief['end_date'] or 'open'})."
                ),
            }
        elif lease:
            phase = "leased_but_not_covering_date"
            vacant += 1
            vacant_flag = True
            brief = _serialize_lease_brief(lease, day, place_name=prop.name)
            brief = {
                **brief,
                "covers_today": False,
                "vacant_today": True,
                "occupied_today": False,
                "summary": (
                    f"Vacant on {day.isoformat()}. Has lease "
                    f"{brief.get('lease_number')} "
                    f"{brief['start_date']} → {brief['end_date'] or 'open'} "
                    f"but that term does not cover this date."
                ),
            }
        else:
            phase = "vacant"
            vacant += 1
            vacant_flag = True
            brief = None

        rows.append(
            {
                "property": prop.name,
                "group": prop.group.name if prop.group_id else None,
                "on_date": day.isoformat(),
                "phase": phase,
                "vacant_on_date": vacant_flag,
                "occupied_on_date": not vacant_flag,
                "lease": brief,
            }
        )

    # Label relative to "today" for wording help
    if day.year == today.year and day.month == today.month + 1:
        rel = "next_calendar_month"
    elif day.year == today.year + 1 and today.month == 12 and day.month == 1:
        rel = "next_calendar_month"
    elif day > today:
        rel = "future_date"
    elif day < today:
        rel = "past_date"
    else:
        rel = "today"

    return {
        "as_of_today": today.isoformat(),
        "on_date": day.isoformat(),
        "relative": rel,
        "calendar_month": day.strftime("%Y-%m"),
        "month_label": day.strftime("%B %Y"),
        "counts": {
            "occupied_on_date": occupied,
            "vacant_on_date": vacant,
            "total": len(rows),
        },
        "listings": rows,
        "rules": {
            "next_month": (
                f"If today is {today.isoformat()} ({today.strftime('%B')}), "
                f"'next month' means the following calendar month — not 'this month'. "
                f"Pass on_date as the 1st of that month (e.g. 2026-08-01)."
            ),
            "wording": (
                "Use vacant_on_date / occupied_on_date for the asked day. "
                "Do not say 'vacant today' when answering about a future month."
            ),
        },
    }


def state_of_the_union(landlord) -> dict:
    from rentium.attention.service import compute_attention
    from rentium.leases.models import Lease
    from rentium.ledger import services
    from rentium.ledger.models import INCOME_CHARGE_TYPES, LedgerEntry
    from rentium.maintenance.models import WorkOrder

    today = date.today()
    start, end = _month_bounds(today)

    lease_counts = {
        "active": Lease.objects.filter(
            landlord=landlord, status=Lease.LeaseStatus.ACTIVE
        ).count(),
        "awaiting_signatures": Lease.objects.filter(
            landlord=landlord, status=Lease.LeaseStatus.PENDING_SIGNATURES
        ).count(),
    }

    open_charges = LedgerEntry.objects.with_settlement().filter(
        landlord=landlord,
        entry_type__in=INCOME_CHARGE_TYPES,
        reversed_by__isnull=True,
        due_date__lte=today,
        outstanding__gt=0,
    )
    agg = open_charges.aggregate(total=Sum("outstanding"), count=Count("id"))
    overdue_count = open_charges.filter(due_date__lt=today).count()

    open_work = (
        WorkOrder.objects.filter(property__landlord=landlord)
        .exclude(status__in=[WorkOrder.Status.COMPLETED, WorkOrder.Status.CANCELLED])
        .count()
    )

    items = compute_attention(landlord)
    severity_counts = {"urgent": 0, "soon": 0, "info": 0}
    for item in items:
        severity_counts[item.severity] = severity_counts.get(item.severity, 0) + 1

    inventory = property_inventory(landlord)
    occ = inventory["counts"]["by_occupancy"]
    appointments = list_appointments(landlord, days_ahead=90)
    this_month = month_money(landlord, start, end)
    deposits = services.deposits_held(landlord)
    next_ch = services.next_upcoming_charge(landlord)
    layout = inventory.get("layout") or property_layout(landlord)

    # Compact lease rollup for "who's rented / when does X move out" without
    # a second tool call.
    rented_rows = []
    for row in inventory["rooms"] + inventory["complete_units"]:
        occ_row = row.get("occupancy") or {}
        lease_brief = occ_row.get("lease")
        if not lease_brief:
            continue
        rented_rows.append(
            {
                "property": row["name"],
                "listing_status": row.get("listing_status") or row.get("status"),
                "occupancy_phase": occ_row.get("phase"),
                "vacant_today": occ_row.get("vacant_today"),
                "occupied_today": occ_row.get("occupied_today"),
                "is_rented_or_committed": occ_row.get("is_rented_or_committed"),
                "has_future_commitment": occ_row.get("has_future_commitment"),
                "term_overlaps_this_calendar_month": occ_row.get(
                    "term_overlaps_this_calendar_month"
                ),
                "lease_id": lease_brief.get("lease_id"),
                "lease_number": lease_brief.get("lease_number"),
                "agreement_type": lease_brief.get("agreement_type"),
                "start_date": lease_brief.get("start_date"),
                "end_date": lease_brief.get("end_date"),
                "move_out_date": lease_brief.get("move_out_date"),
                "monthly_rent": lease_brief.get("monthly_rent"),
                "tenants": [
                    t.get("name") for t in (lease_brief.get("tenants") or [])
                ],
                "summary": lease_brief.get("summary"),
            }
        )

    draft_count = Lease.objects.filter(
        landlord=landlord, status=Lease.LeaseStatus.DRAFT
    ).count()

    # Matches the landlord dashboard hero numbers — models must copy these.
    dashboard_truth = {
        "as_of": today.isoformat(),
        "total_listings": inventory["counts"]["total_listings"],
        "full_units": inventory["counts"]["complete_units"],
        "rooms": inventory["counts"]["rooms"],
        "occupied_today": f"{occ['occupied_today']}/{inventory['counts']['total_listings']}",
        "occupied_today_count": occ["occupied_today"],
        "vacant_today_count": occ["vacant_today"],
        "expected_this_month": this_month["expected_income"],
        "collected_this_month": this_month["collected_income"],
        "deposits_collected_this_month": this_month["deposits_collected"],
        "expenses_this_month": this_month["expenses"],
        "expenses_not_yet_from_bank": this_month[
            "expenses_not_yet_taken_from_bank"
        ],
        "deposits_held": str(deposits),
        "outstanding_total": str(agg["total"] or Decimal("0.00")),
        "outstanding_count": agg["count"] or 0,
        "overdue_count": overdue_count,
        "active_leases": lease_counts["active"],
        "draft_leases": draft_count,
        "next_charge": next_ch,
        "upcoming_viewings": appointments["counts"][
            "upcoming_scheduled_or_requested"
        ],
        "COPY_EXACTLY": (
            "For portfolio totals (listings count, expected/collected rent, "
            "deposits held, outstanding, expense totals) copy these exactly. "
            "Do not invent $4850 rent, $5000 deposits, or fake expense lines. "
            "outstanding_total is unpaid charges due on or before as_of — NOT next_charge. "
            "next_charge is a future scheduled amount and is not owed yet if "
            "outstanding_total is 0.00."
        ),
    }

    return {
        "as_of": today.isoformat(),
        "dashboard_truth": dashboard_truth,
        "portfolio": {
            # Keep "properties" as total listings for backwards compatibility.
            "properties": inventory["counts"]["total_listings"],
            "rooms": inventory["counts"]["rooms"],
            "complete_units": inventory["counts"]["complete_units"],
            "by_status": inventory["counts"]["by_status"],
            "by_listing_status": inventory["counts"]["by_listing_status"],
            "by_occupancy": occ,
            "room_names": [r["name"] for r in inventory["rooms"]],
            "unit_names": [u["name"] for u in inventory["complete_units"]],
            "leases": lease_counts,
            "occupied_today": occ["occupied_today"],
            "vacant_today": occ["vacant_today"],
            "has_lease_commitment": occ["has_lease_commitment"],
            # Legacy aliases — prefer the fields above for wording.
            "rented_or_committed": occ["rented_or_committed"],
            "vacant_unleased": occ["truly_unleased"],
            "upcoming_viewings": appointments["counts"][
                "upcoming_scheduled_or_requested"
            ],
        },
        "layout": layout,
        # Full inventory so "how many rooms / list rooms" can be answered
        # from portfolio_snapshot without a second tool call.
        "listings": inventory,
        "rented_listings": rented_rows,
        "upcoming_appointments": appointments["appointments"][:15],
        "appointments_summary": appointments["counts"],
        "this_month": this_month,
        "this_month_expenses": this_month.get("expense_lines", []),
        "outstanding": {
            "total": str(agg["total"] or Decimal("0.00")),
            "count": agg["count"] or 0,
            "overdue_count": overdue_count,
        },
        "deposits_held": str(deposits),
        "next_charge": next_ch,
        "open_work_orders": open_work,
        "attention": {
            "counts": severity_counts,
            "top": [item.as_dict() for item in items[:5]],
        },
    }


def list_appointments(
    landlord,
    *,
    day: str = "",
    days_ahead: int = 60,
    include_past: bool = False,
    limit: int = 50,
) -> dict:
    """Viewings / showings / contractor visits for this landlord.

    Filters: optional day 'YYYY-MM-DD'; otherwise from now (or past if
    include_past) through days_ahead. Statuses REQUESTED and SCHEDULED are
    the ones landlords usually mean by "viewings".
    """
    from datetime import datetime, time, timedelta

    from django.utils import timezone

    from rentium.appointments.models import Appointment

    now = timezone.now()
    today = timezone.localdate()
    qs = Appointment.objects.filter(landlord=landlord).select_related(
        "property", "lease"
    )

    from zoneinfo import ZoneInfo

    display_tz = ZoneInfo("America/Vancouver")
    day_s = (day or "").strip()
    if day_s:
        try:
            target = date.fromisoformat(day_s)
        except ValueError:
            return {
                "error": f"day must be YYYY-MM-DD, got {day!r}",
                "as_of": today.isoformat(),
                "appointments": [],
                "counts": {},
            }
        # Vancouver calendar day (not bare UTC midnight).
        start_dt = datetime.combine(target, time.min, tzinfo=display_tz)
        end_dt = datetime.combine(target, time.max, tzinfo=display_tz)
        qs = qs.filter(starts_at__gte=start_dt, starts_at__lte=end_dt)
        window = {"day": target.isoformat(), "timezone": "America/Vancouver"}
    else:
        ahead = max(1, min(int(days_ahead or 60), 365))
        end_dt = now + timedelta(days=ahead)
        if include_past:
            start_dt = now - timedelta(days=30)
            qs = qs.filter(starts_at__gte=start_dt, starts_at__lte=end_dt)
        else:
            start_dt = datetime.combine(today, time.min, tzinfo=display_tz)
            qs = qs.filter(starts_at__gte=start_dt, starts_at__lte=end_dt)
        window = {
            "from": start_dt.isoformat(),
            "to": end_dt.isoformat(),
            "days_ahead": ahead,
            "timezone": "America/Vancouver",
        }

    qs = qs.order_by("starts_at")[: max(1, min(limit, 100))]
    rows = []
    counts = {
        "total_returned": 0,
        "upcoming_scheduled_or_requested": 0,
        "by_status": {},
        "by_kind": {},
    }
    as_of_day = today  # local calendar day for relative labels
    for appt in qs:
        van_start = appt.starts_at.astimezone(display_tz)
        appt_day = van_start.date()
        time_12 = van_start.strftime("%I:%M %p").lstrip("0")
        days_until = (appt_day - as_of_day).days
        if days_until == 0:
            relative = "today"
        elif days_until == 1:
            relative = "tomorrow"
        elif days_until > 1:
            relative = f"in_{days_until}_days"
        else:
            relative = f"{abs(days_until)}_days_ago"
        row = {
            "id": str(appt.pk),
            "kind": appt.kind,
            "kind_display": appt.get_kind_display(),
            "status": appt.status,
            "status_display": appt.get_status_display(),
            "property": appt.property.name if appt.property_id else "",
            "property_id": str(appt.property_id) if appt.property_id else None,
            "starts_at": appt.starts_at.isoformat(),
            "starts_at_utc": appt.starts_at.astimezone(ZoneInfo("UTC")).strftime(
                "%Y-%m-%d %H:%M UTC"
            ),
            "starts_at_vancouver": van_start.strftime("%Y-%m-%d %H:%M %Z"),
            "starts_at_local": van_start.strftime("%Y-%m-%d %H:%M"),
            "date": appt_day.isoformat(),
            "time_local": van_start.strftime("%H:%M"),
            "time_display": time_12,
            "weekday": van_start.strftime("%A"),
            "days_from_as_of": days_until,
            "relative_to_as_of": relative,
            "timezone_note": (
                "Display times in America/Vancouver. Always use date+weekday+time_display "
                "from this payload — if count>0 for a day, never say there are no viewings. "
                "Use relative_to_as_of for 'today/tomorrow' only; never invent weekdays."
            ),
            "ends_at": appt.ends_at.isoformat() if appt.ends_at else None,
            "contact_name": appt.contact_name or "",
            "contact_email": appt.contact_email or "",
            "notes": (appt.notes or "")[:200],
            "lease_id": str(appt.lease_id) if appt.lease_id else None,
        }
        rows.append(row)
        counts["total_returned"] += 1
        counts["by_status"][appt.status] = counts["by_status"].get(appt.status, 0) + 1
        counts["by_kind"][appt.kind] = counts["by_kind"].get(appt.kind, 0) + 1
        if appt.status in (
            Appointment.Status.SCHEDULED,
            Appointment.Status.REQUESTED,
        ):
            counts["upcoming_scheduled_or_requested"] += 1

    return {
        "as_of": today.isoformat(),
        "now": now.isoformat(),
        "window": window,
        "counts": counts,
        "appointments": rows,
        "rules": {
            "viewings": (
                "Viewings/showings are appointments with kind=VIEWING. "
                "Scheduled and Requested are the live ones. "
                "Use date + weekday + time_local when the user says 'Thursday' "
                "or a calendar date."
            ),
        },
    }


def list_leases(
    landlord, *, include_ended: bool = False, include_drafts: bool = True, limit: int = 50
) -> dict:
    """Leases for this landlord (active + pending; drafts optional)."""
    from rentium.leases.models import Lease

    today = date.today()
    statuses = [
        Lease.LeaseStatus.ACTIVE,
        Lease.LeaseStatus.PENDING_SIGNATURES,
    ]
    if include_drafts:
        statuses.append(Lease.LeaseStatus.DRAFT)
    if include_ended:
        statuses.extend(
            [
                Lease.LeaseStatus.EXPIRED,
                Lease.LeaseStatus.TERMINATED,
                Lease.LeaseStatus.RENEWED,
            ]
        )

    qs = (
        Lease.objects.filter(landlord=landlord, status__in=statuses)
        .select_related("property", "group")
        .prefetch_related("lease_tenants__tenant__user")
        .order_by("-start_date")[: max(1, min(limit, 100))]
    )
    rows = []
    for lease in qs:
        brief = _serialize_lease_brief(lease, today)
        brief["is_draft"] = lease.status == Lease.LeaseStatus.DRAFT
        if brief["is_draft"]:
            brief["summary"] = (
                f"DRAFT lease only — not signed/active. Do not count as rented. "
                f"Agreement: {brief.get('agreement_type')}. "
                f"Term on draft: {brief['start_date']} → {brief['end_date'] or 'open'}."
            )
            brief["rented"] = False
            brief["has_lease_commitment"] = False
        rows.append(brief)
    return {
        "as_of": today.isoformat(),
        "count": len(rows),
        "leases": rows,
        "rules": {
            "rented_out": (
                "Only ACTIVE or PENDING_SIGNATURES count as rented/committed. "
                "DRAFT leases are unfinished paperwork — not occupancy."
            ),
            "lease_number": (
                "lease_number is on each row (e.g. RMT905081-BD24). Always report it "
                "when the user asks for the lease number."
            ),
        },
    }
