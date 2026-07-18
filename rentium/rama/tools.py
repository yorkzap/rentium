"""
RAMA's tool surface: typed, side-effect-free read functions over the same
service layer the dashboard uses. The model's job is to pick a function and
fill its arguments; every number, date, and record comes from here.

Contract for every function:
- First parameter is `landlord`, injected by the registry from the
  authenticated session — never from model output. Cross-landlord
  retrieval is therefore impossible at this layer.
- Read-only. No writes, no domain events, no emails.
- Returns a JSON-serializable dict. Errors come back as {"error": ...}
  so the model can explain them instead of the request crashing.
- The docstring IS the tool description the model sees — write it for
  the model.
"""

from __future__ import annotations

from datetime import date


def _month_bounds(month: str):
    """Parse 'YYYY-MM' (empty = current month) into [start, end)."""
    from .union import _month_bounds as bounds

    if not month:
        return bounds(date.today())
    try:
        year, mon = month.split("-")
        return bounds(date(int(year), int(mon), 1))
    except (ValueError, TypeError):
        raise ValueError(f"month must look like 2026-07, got {month!r}")


def portfolio_snapshot(landlord) -> dict:
    """Dashboard ground truth + full portfolio. ALWAYS call this (or re-call it)
    for totals — never invent numbers from memory.

    dashboard_truth has the exact figures the UI shows: total_listings, occupied
    today, expected/collected this month, deposits_held, expenses, next_charge.
    layout.groups vs layout.standalone_units explains shared rooms vs garden suite.
    rented_listings has lease_number, agreement_type, vacant_today.
    upcoming_appointments has viewings with date/weekday/time_display.

    TIME-AWARE: vacant_today vs leased_future; listing Available ≠ vacant of lease.
    MONEY: copy expense_lines descriptions/amounts exactly (never invent Hydro)."""
    from .union import state_of_the_union

    return state_of_the_union(landlord)


def list_properties(landlord) -> dict:
    """Every listing: name, layout group, primary_type (Garden Suite / Private Room),
    suggested_lease_if_created, occupancy with lease_number when committed.

    'How many properties' = counts.total_listings (not 1 street address).
    'Same unit?' = layout.groups (shared rooms) vs standalone complete units."""
    from .union import property_inventory

    return property_inventory(landlord)


def occupancy_as_of(landlord, on_date: str = "") -> dict:
    """Occupancy of every listing on one calendar day (YYYY-MM-DD). Empty = today.
    Use for 'next month', 'in August', 'on 2026-08-01'. When user says next month
    and today is July, pass on_date=YYYY-08-01 (August), not July."""
    from .union import occupancy_as_of as _fn

    return _fn(landlord, on_date=(on_date or "").strip())


def list_leases(landlord, include_ended: str = "") -> dict:
    """List leases for this landlord (active and pending signatures by default).
    Each row has occupancy_phase, vacant_today, start/end dates, tenants, and a
    plain-language summary. Pass include_ended as '1' or 'true' to also include
    expired/terminated/renewed leases."""
    from .union import list_leases as _list

    flag = str(include_ended or "").strip().lower() in ("1", "true", "yes", "y")
    return _list(landlord, include_ended=flag)


def list_appointments(landlord, day: str = "", days_ahead: str = "60") -> dict:
    """List viewings, showings, and contractor appointments. Optional day as
    YYYY-MM-DD (e.g. 2026-07-30) filters that calendar day; otherwise returns
    upcoming appointments for days_ahead days (default 60).

    Use for "any viewings?", "what's on Thursday?", "showings on July 30".
    Each row has property, status (SCHEDULED/REQUESTED/…), date, weekday,
    time_local, and kind (VIEWING, etc.). Never invent appointments — if the
    list is empty for that day, say none are in the system."""
    from .union import list_appointments as _list

    try:
        ahead = int(str(days_ahead or "60").strip() or "60")
    except ValueError:
        ahead = 60
    return _list(landlord, day=(day or "").strip(), days_ahead=ahead)


def attention_items(landlord) -> dict:
    """Everything that currently needs the landlord's attention, most urgent
    first: missing or undelivered condition inspections, stalled lease
    signatures, expiring fixed terms, overdue rent, and new or SLA-breached
    maintenance requests."""
    from rentium.attention.service import compute_attention

    return {"items": [item.as_dict() for item in compute_attention(landlord)]}


def resolve_person(landlord, name: str) -> dict:
    """Find tenants in this landlord's portfolio by partial name or email.
    Returns candidates with their lease context. If more than one candidate
    matches, ask the user which one they mean — never guess between
    people."""
    from django.db.models import Q

    from rentium.leases.models import LeaseTenant

    hits = (
        LeaseTenant.objects.filter(lease__landlord=landlord)
        .filter(
            Q(invited_name__icontains=name)
            | Q(invited_email__icontains=name)
            | Q(tenant__user__name__icontains=name)
        )
        .select_related("tenant__user", "lease", "lease__property", "lease__group")
        .order_by("-lease__start_date")[:10]
    )
    candidates = []
    for lt in hits:
        place = (
            lt.lease.property.name
            if lt.lease.property
            else (lt.lease.group.name if lt.lease.group else "")
        )
        email = lt.invited_email or (
            lt.tenant.user.email if lt.tenant_id else ""
        )
        candidates.append(
            {
                "name": lt.display_name,
                "email": email,
                "lease_id": str(lt.lease_id),
                "lease_status": lt.lease.status,
                "property": place,
                "is_primary_tenant": lt.is_primary_tenant,
                "has_signed": lt.has_signed,
            }
        )
    return {"query": name, "candidates": candidates}


def lease_state(landlord, lease_id: str) -> dict:
    """Full state of one lease: lease_number, agreement_type (Standard Roommate
    Agreement / RTB-1), term_shape, dates, rent, deposits, tenants, bills_included,
    etransfer_email, occupancy. Get lease_id from list_properties / list_leases /
    portfolio_snapshot. 'Lease type?' → agreement_type. 'Lease number?' → lease_number."""
    from django.core.exceptions import ValidationError

    from rentium.leases.models import Lease

    from .union import _serialize_lease_brief

    try:
        lease = (
            Lease.objects.select_related("property", "group")
            .prefetch_related("lease_tenants__tenant__user")
            .get(pk=lease_id, landlord=landlord)
        )
    except (Lease.DoesNotExist, ValidationError, ValueError):
        return {"error": f"No lease {lease_id!r} in this portfolio."}

    today = date.today()
    brief = _serialize_lease_brief(lease, today)
    place = brief["property"]
    listing_status = lease.property.status if lease.property_id else None
    bills = lease.bills_included or {}
    bills_summary = []
    if isinstance(bills, dict):
        for key, details in bills.items():
            if not isinstance(details, dict):
                continue
            bills_summary.append(
                {
                    "bill": key,
                    "included_in_rent": bool(details.get("included")),
                    "provider": details.get("provider") or "",
                }
            )
    try:
        etransfer = lease.get_effective_etransfer_email() or ""
    except Exception:  # noqa: BLE001
        etransfer = lease.etransfer_email or ""
    return {
        **brief,
        "property": place,
        "listing_status": listing_status,
        "security_deposit": str(lease.security_deposit),
        "pet_deposit": str(lease.pet_deposit),
        "bills_included": bills,
        "bills_summary": bills_summary,
        "etransfer_email": etransfer,
        "tenants": [
            {
                "name": lt.display_name,
                "is_primary": lt.is_primary_tenant,
                "has_signed": lt.has_signed,
                "declined": lt.declined,
            }
            for lt in lease.lease_tenants.all()
        ],
        "note": (
            "lease_number is the human id (e.g. RMT…). agreement_type is the form. "
            "term_shape is fixed_term vs month_to_month. bills_summary answers "
            "'bills included?'. etransfer_email answers landlord e-transfer destination."
        ),
    }


def charge_status(landlord, lease_id: str, month: str = "") -> dict:
    """Charges on one lease for one month (format 2026-07; empty = current
    month) and how much of each has been paid. Answers "did X pay rent this
    month?" — call resolve_person first to get the lease_id."""
    from django.core.exceptions import ValidationError

    from rentium.ledger.models import CHARGE_TYPES, LedgerEntry

    try:
        start, end = _month_bounds(month)
    except ValueError as exc:
        return {"error": str(exc)}

    try:
        charges = list(
            LedgerEntry.objects.with_settlement()
            .filter(
                landlord=landlord,
                lease_id=lease_id,
                entry_type__in=CHARGE_TYPES,
                reversed_by__isnull=True,
                due_date__gte=start,
                due_date__lt=end,
            )
            .order_by("due_date", "created_at")
        )
    except (ValidationError, ValueError):
        return {"error": f"lease_id {lease_id!r} is not a valid id."}

    today = date.today()
    rows = []
    for charge in charges:
        outstanding = charge.outstanding
        if outstanding <= 0:
            status = "paid"
        elif charge.settled_amount > 0:
            status = "partially_paid"
        else:
            status = "unpaid"
        rows.append(
            {
                "description": charge.description,
                "type": charge.entry_type,
                "amount": str(charge.amount),
                "due_date": charge.due_date.isoformat(),
                "paid": str(charge.settled_amount),
                "outstanding": str(outstanding),
                "status": status,
                "overdue": bool(outstanding > 0 and charge.due_date < today),
            }
        )
    return {"lease_id": str(lease_id), "month": start.strftime("%Y-%m"), "charges": rows}


def month_money(landlord, month: str = "") -> dict:
    """Money for one month (format YYYY-MM e.g. 2026-07; empty = current month
    from as_of / today): expected vs collected income, expense total, expense
    line items, amount not yet taken from bank, net, deposits. Always use the
    year returned in 'month' / 'label' — never invent 2025 when as_of is 2026."""
    from .union import month_money as compute

    try:
        start, end = _month_bounds(month)
    except ValueError as exc:
        return {"error": str(exc)}
    return compute(landlord, start, end)


def list_expenses(
    landlord,
    month: str = "",
    day: str = "",
    property_query: str = "",
    amount: str = "",
) -> dict:
    """List expense ledger lines. Optional month (YYYY-MM), day (YYYY-MM-DD for
    'today'), property_query (e.g. 'Room E'), amount (e.g. '600').

    Use for "expenses today", "expenses for room E", "$600 expense", "utilities
    this month". If day has zero lines, still read this_month_expenses — bills
    often use period end dates, not today. bank_status NOT_YET_TAKEN = not left bank."""
    from .union import list_expenses as _list

    return _list(
        landlord,
        month=(month or "").strip(),
        day=(day or "").strip(),
        property_query=(property_query or "").strip(),
        amount=(amount or "").strip(),
    )


def deposits_summary(landlord) -> dict:
    """Total security/pet deposits currently held as a refundable liability
    (payments received on deposit charges, minus deposits returned)."""
    from rentium.ledger import services

    return {"deposits_held": str(services.deposits_held(landlord))}


def next_charge(landlord) -> dict:
    """The earliest not-yet-settled charge with a future due date across the
    portfolio, or null if the billing calendar is empty."""
    from rentium.ledger import services

    return {"next_charge": services.next_upcoming_charge(landlord)}


def open_work_orders(landlord) -> dict:
    """Open maintenance work orders only (not completed/cancelled). Prefer
    list_work_orders for full detail (category, contractor, SLA breach, costs)."""
    from .domain_reads import list_work_orders

    return list_work_orders(landlord, include_closed=False)


def list_work_orders(
    landlord,
    include_closed: str = "",
    property_query: str = "",
    status: str = "",
    priority: str = "",
) -> dict:
    """Strong work-order list for UI-style and natural questions.

    include_closed: '1' to include COMPLETED/CANCELLED. property_query filters
    by property name or title (e.g. 'Room E', 'plumbing'). status/priority
    optional (NEW, IN_PROGRESS, HIGH, EMERGENCY…).

    Returns title, property, area, status, priority, category, origin, SLA,
    contractor, cost, is_rta_emergency. Empty list = no work orders — say so."""
    from .domain_reads import list_work_orders as _fn

    return _fn(
        landlord,
        include_closed=str(include_closed or "").strip().lower()
        in ("1", "true", "yes", "y"),
        property_query=(property_query or "").strip(),
        status=(status or "").strip(),
        priority=(priority or "").strip(),
    )


def list_inquiries(
    landlord,
    status: str = "",
    property_query: str = "",
    include_archived: str = "",
) -> dict:
    """Public listing interest inquiries (name, email, phone, message, move-in
    target, status NEW/REPLIED). Not the same as signed lease applications.

    Use for 'any inquiries?', 'who messaged about Garden Suite?', 'new leads'.
    If empty, say none in the system."""
    from .domain_reads import list_inquiries as _fn

    return _fn(
        landlord,
        status=(status or "").strip(),
        property_query=(property_query or "").strip(),
        include_archived=str(include_archived or "").strip().lower()
        in ("1", "true", "yes"),
    )


def list_conversations(landlord, tenant_query: str = "") -> dict:
    """Message threads between this landlord and tenants. Each has subject,
    tenant name/email, property, unread count, last message preview.

    Use for 'any messages?', 'threads with someone'. Then list_messages for
    full bodies."""
    from .domain_reads import list_conversations as _fn

    return _fn(landlord, tenant_query=(tenant_query or "").strip())


def list_messages(
    landlord,
    conversation_id: str = "",
    tenant_query: str = "",
) -> dict:
    """Messages in one conversation_id, or recent messages if no id.
    Bodies truncated; sender is landlord or tenant. Read-only."""
    from .domain_reads import list_messages as _fn

    return _fn(
        landlord,
        conversation_id=(conversation_id or "").strip(),
        tenant_query=(tenant_query or "").strip(),
    )


def list_inspections(
    landlord,
    property_query: str = "",
    status: str = "",
    include_items: str = "1",
) -> dict:
    """Condition inspections (RTB-27 style): status, dates, signatures, plus
    checklist_by_section (per-room line items with move-in condition codes).

    property_query e.g. 'Room E'. include_items default on. Use for
    'move-in checklist', 'inspection items Bedroom', attention beyond flags."""
    from .domain_reads import list_inspections as _fn

    return _fn(
        landlord,
        property_query=(property_query or "").strip(),
        status=(status or "").strip(),
        include_items=str(include_items or "1").strip().lower()
        not in ("0", "false", "no"),
    )


def list_move_events(
    landlord,
    days_ahead: str = "120",
    days_past: str = "30",
    property_query: str = "",
) -> dict:
    """Move-ins (lease start dates) and move-outs (lease end + move-out requests).

    Use for 'when is move-in for Room E?', 'any move-outs coming?',
    'who's moving in August?'. Room E lease start 2026-08-01 is a move-in."""
    from .domain_reads import list_move_events as _fn

    try:
        ahead = int(str(days_ahead or "120").strip() or "120")
    except ValueError:
        ahead = 120
    try:
        past = int(str(days_past or "30").strip() or "30")
    except ValueError:
        past = 30
    return _fn(
        landlord,
        days_ahead=ahead,
        days_past=past,
        property_query=(property_query or "").strip(),
    )


def list_inventory(landlord, property_query: str = "", include_shared: str = "1") -> dict:
    """Furniture and inventory: private items per room/unit + shared items for
    household groups (kitchen etc.). name, qty, condition, location, property/group.

    Use for 'what's in Room E?', 'inventory for Side Unit', 'any furniture listed?'."""
    from .domain_reads import list_inventory as _fn

    return _fn(
        landlord,
        property_query=(property_query or "").strip(),
        include_shared=str(include_shared or "1").strip().lower()
        not in ("0", "false", "no"),
    )


def charge_schedule(
    landlord,
    property_query: str = "",
    month: str = "",
    include_paid: str = "1",
) -> dict:
    """Charge schedule for a property or whole portfolio: rent, deposits, fees
    with due_date, amount, paid, outstanding, status (scheduled/unpaid/paid).

    Empty month = this month through ~6 months ahead. month=YYYY-MM narrows.
    property_query e.g. 'Room E'. Scheduled future rent is NOT portfolio
    outstanding until due."""
    from .domain_reads import charge_schedule as _fn

    return _fn(
        landlord,
        property_query=(property_query or "").strip(),
        month=(month or "").strip(),
        include_paid=str(include_paid or "1").strip().lower()
        not in ("0", "false", "no"),
    )


def list_tenants(
    landlord,
    query: str = "",
    include_past: str = "1",
) -> dict:
    """Tenants as people with full lease history across properties.

    Each person: name, email, leases[] with property, dates, status, signed.
    Use for 'who is on Room E?', 'tenant history for someone@…', 'list tenants'."""
    from .domain_reads import list_tenants as _fn

    return _fn(
        landlord,
        query=(query or "").strip(),
        include_past=str(include_past or "1").strip().lower()
        not in ("0", "false", "no"),
    )


def tenant_history(landlord, query: str) -> dict:
    """Deep history for one tenant by name or email (all leases over time).
    If multiple people match, list them — do not merge different people."""
    from .domain_reads import tenant_history as _fn

    return _fn(landlord, query=(query or "").strip())


def list_documents(
    landlord,
    property_query: str = "",
    lease_id: str = "",
) -> dict:
    """Documents/PDFs metadata: lease attachments (title, file name) and whether
    lease agreement PDFs exist. Does NOT return file contents.

    Use for 'any documents on Room E?', 'PDFs for lease RMT…', 'uploaded files'."""
    from .domain_reads import list_documents as _fn

    return _fn(
        landlord,
        property_query=(property_query or "").strip(),
        lease_id=(lease_id or "").strip(),
    )


# ---------------------------------------------------------------------------
# Write actions (require confirm=yes)
# ---------------------------------------------------------------------------


def create_work_order(
    landlord,
    property_query: str,
    title: str,
    description: str = "",
    priority: str = "MEDIUM",
    category: str = "OTHER",
    confirm: str = "",
) -> dict:
    """Create a maintenance work order. ALWAYS call once WITHOUT confirm to
    preview, show the landlord, then call again with confirm=yes to save.

    property_query = listing name (e.g. 'Room E'). priority HIGH/MEDIUM/LOW/EMERGENCY.
    category PLUMBING/ELECTRICAL/…/OTHER."""
    from .domain_actions import create_work_order as _fn

    return _fn(
        landlord,
        property_query=property_query,
        title=title,
        description=description,
        priority=priority,
        category=category,
        confirm=confirm,
    )


def transition_work_order(
    landlord,
    new_status: str,
    work_order_id: str = "",
    title_query: str = "",
    confirm: str = "",
) -> dict:
    """Change work order status (NEW→SCHEDULED→IN_PROGRESS→COMPLETED/CANCELLED).
    Preview first; confirm=yes to apply. Pass work_order_id or title_query."""
    from .domain_actions import transition_work_order as _fn

    return _fn(
        landlord,
        work_order_id=work_order_id,
        title_query=title_query,
        new_status=new_status,
        confirm=confirm,
    )


def mark_inquiry_replied(
    landlord, inquiry_id: str = "", name_query: str = "", confirm: str = ""
) -> dict:
    """Mark a listing inquiry as REPLIED. Preview first; confirm=yes to save."""
    from .domain_actions import mark_inquiry_replied as _fn

    return _fn(
        landlord, inquiry_id=inquiry_id, name_query=name_query, confirm=confirm
    )


def send_tenant_message(
    landlord,
    body: str,
    tenant_query: str = "",
    conversation_id: str = "",
    property_query: str = "",
    subject: str = "",
    confirm: str = "",
) -> dict:
    """Send a message to a tenant. Preview first; confirm=yes to send.
    Pass conversation_id or tenant_query (name/email of linked tenant)."""
    from .domain_actions import send_tenant_message as _fn

    return _fn(
        landlord,
        body=body,
        tenant_query=tenant_query,
        conversation_id=conversation_id,
        property_query=property_query,
        subject=subject,
        confirm=confirm,
    )


def mark_messages_read(
    landlord, conversation_id: str = "", confirm: str = ""
) -> dict:
    """Mark unread tenant messages as read. Empty conversation_id = all threads.
    Preview first; confirm=yes to apply."""
    from .domain_actions import mark_messages_read as _fn

    return _fn(landlord, conversation_id=conversation_id, confirm=confirm)


def schedule_viewing(
    landlord,
    property_query: str,
    when: str,
    contact_name: str = "",
    contact_email: str = "",
    notes: str = "",
    confirm: str = "",
) -> dict:
    """Schedule a property SHOWING/viewing for prospective tenants only.
    when like '2026-08-05 14:00'.
    Do NOT use for move-in condition inspections — use create_condition_inspection.
    Preview first; confirm=yes to create."""
    from .domain_actions import schedule_viewing as _fn

    return _fn(
        landlord,
        property_query=property_query,
        when=when,
        contact_name=contact_name,
        contact_email=contact_email,
        notes=notes,
        confirm=confirm,
    )


def create_condition_inspection(
    landlord,
    property_query: str = "",
    lease_number: str = "",
    tenant_email: str = "",
    confirm: str = "",
) -> dict:
    """Create a real RTB-style condition inspection (lease → Condition Inspections).
    Requires at least one tenant on the lease. NOT a calendar viewing.
    Preview first; confirm=yes."""
    from .domain_actions import create_condition_inspection as _fn

    return _fn(
        landlord,
        property_query=property_query,
        lease_number=lease_number,
        tenant_email=tenant_email,
        confirm=confirm,
    )


def lease_pdf_info(
    landlord,
    property_query: str = "",
    lease_number: str = "",
) -> dict:
    """How to download a lease PDF. Always available via /api/leases/<id>/pdf/
    even when document_file is empty — never say 'no PDF' for an existing lease."""
    from .domain_actions import lease_pdf_info as _fn

    return _fn(
        landlord, property_query=property_query, lease_number=lease_number
    )


def bulk_add_inventory(
    landlord,
    property_query: str,
    items: str,
    confirm: str = "",
) -> dict:
    """Add multiple private inventory items at once.
    items e.g. 'Single bed, Mattress, Desk'. Preview; confirm=yes."""
    from .domain_actions import bulk_add_inventory as _fn

    return _fn(
        landlord, property_query=property_query, items=items, confirm=confirm
    )


def create_expense(
    landlord,
    amount: str,
    description: str,
    property_query: str = "",
    effective_date: str = "",
    confirm: str = "",
) -> dict:
    """Record a landlord expense. amount e.g. '75.00'. Preview first; confirm=yes."""
    from .domain_actions import create_expense as _fn

    return _fn(
        landlord,
        amount=amount,
        description=description,
        property_query=property_query,
        effective_date=effective_date,
        confirm=confirm,
    )


def invite_tenant_to_lease(
    landlord,
    email: str,
    name: str,
    property_query: str = "",
    lease_number: str = "",
    rent_amount: str = "",
    is_primary: str = "auto",
    phone: str = "",
    mode: str = "add",
    replace_email: str = "",
    replace_name: str = "",
    confirm: str = "",
) -> dict:
    """Invite someone to sign a lease. Rent auto-splits (do NOT invent $1000 each).
    mode=replace + replace_name/email removes that pending invite first.
    Prefer replace_lease_invite for 'suspend X invite Y'. Preview; confirm=yes."""
    from .domain_actions import invite_tenant_to_lease as _fn

    return _fn(
        landlord,
        property_query=property_query,
        lease_number=lease_number,
        email=email,
        name=name,
        rent_amount=rent_amount,
        is_primary=is_primary,
        phone=phone,
        mode=mode,
        replace_email=replace_email,
        replace_name=replace_name,
        confirm=confirm,
    )


def resend_lease_invite(
    landlord,
    email: str = "",
    property_query: str = "",
    lease_number: str = "",
    confirm: str = "",
) -> dict:
    """Resend an existing pending lease signing invite. Preview first; confirm=yes."""
    from .domain_actions import resend_lease_invite as _fn

    return _fn(
        landlord,
        email=email,
        property_query=property_query,
        lease_number=lease_number,
        confirm=confirm,
    )


def cancel_lease_invite(
    landlord,
    property_query: str = "",
    lease_number: str = "",
    email: str = "",
    name: str = "",
    reason: str = "",
    confirm: str = "",
) -> dict:
    """Suspend/remove a pending (unsigned) lease invite. Preview first; confirm=yes.
    Use when landlord says cancel/suspend/remove invite for X. Does NOT resend."""
    from .domain_actions import cancel_lease_invite as _fn
    return _fn(
        landlord,
        property_query=property_query,
        lease_number=lease_number,
        email=email,
        name=name,
        reason=reason or "Landlord cancelled invite",
        confirm=confirm,
    )


def replace_lease_invite(
    landlord,
    email: str,
    name: str,
    property_query: str = "",
    lease_number: str = "",
    remove_email: str = "",
    remove_name: str = "",
    rent_amount: str = "",
    phone: str = "",
    confirm: str = "",
) -> dict:
    """Replace one pending invite with another person in ONE step.
    remove_name/remove_email = who to suspend; email/name = who to invite instead.
    Rent auto-fills (sole tenant → full lease total_rent). Preview; confirm=yes.
    Use: 'suspend Jabi, invite Movy instead'."""
    from .domain_actions import replace_lease_invite as _fn
    return _fn(
        landlord,
        property_query=property_query,
        lease_number=lease_number,
        remove_email=remove_email,
        remove_name=remove_name,
        email=email,
        name=name,
        rent_amount=rent_amount,
        phone=phone,
        confirm=confirm,
    )


def list_lease_roster(
    landlord, property_query: str = "", lease_number: str = ""
) -> dict:
    """Who is on a lease: pending invites, signed tenants, rent shares, primary.
    Use for 'who should sign?', 'any invitations sent?', rent split questions."""
    from .domain_actions import list_lease_roster as _fn
    return _fn(
        landlord, property_query=property_query, lease_number=lease_number
    )


def add_roommate_to_lease(
    landlord,
    email: str,
    name: str,
    property_query: str = "",
    lease_number: str = "",
    phone: str = "",
    confirm: str = "",
) -> dict:
    """Add a roommate/co-tenant without removing anyone. ALWAYS use this when
    landlord says add another tenant/roommate. Rebalances rent equally
    (e.g. $1000 total → $500 each). Preview; confirm=yes. Never replaces."""
    from .domain_actions import add_roommate_to_lease as _fn
    return _fn(
        landlord,
        email=email,
        name=name,
        property_query=property_query,
        lease_number=lease_number,
        phone=phone,
        confirm=confirm,
    )


def rebalance_lease_rents(
    landlord,
    property_query: str = "",
    lease_number: str = "",
    confirm: str = "",
) -> dict:
    """Equal-split total_rent across unsigned tenants on a lease. Preview; confirm=yes."""
    from .domain_actions import rebalance_lease_rents as _fn
    return _fn(
        landlord,
        property_query=property_query,
        lease_number=lease_number,
        confirm=confirm,
    )


# ---------------------------------------------------------------------------
# CRUD: properties, leases, maintenance update, inventory
# ---------------------------------------------------------------------------


def create_property(
    landlord,
    name: str,
    address: str,
    city: str,
    property_category: str = "ROOM",
    province: str = "BC",
    status: str = "AVAILABLE",
    unit_type: str = "",
    room_type: str = "PRIVATE",
    bedrooms: str = "",
    bathrooms: str = "",
    description: str = "",
    group_name: str = "",
    asking_rent: str = "",
    inventory_items: str = "",
    allow_duplicate_name: str = "0",
    confirm: str = "",
) -> dict:
    """Create a property listing (same rules as Properties UI).
    property_category: ROOM or COMPLETE_UNIT. Rooms need room_type; units need unit_type.
    Optional group_name for rooms only.
    inventory_items: if landlord names furniture (e.g. 'Single bed, Mattress'), pass it
    so 'What's in it' is not empty.
    Refuses exact-name duplicates unless allow_duplicate_name=yes.
    For room+lease+tenant in one ask, prefer setup_room_tenancy instead.
    Preview first; confirm=yes."""
    from .domain_crud import create_property as _fn
    return _fn(
        landlord, name=name, address=address, city=city,
        property_category=property_category, province=province, status=status,
        unit_type=unit_type, room_type=room_type, bedrooms=bedrooms,
        bathrooms=bathrooms, description=description, group_name=group_name,
        asking_rent=asking_rent, inventory_items=inventory_items,
        allow_duplicate_name=allow_duplicate_name, confirm=confirm,
    )


def setup_room_tenancy(
    landlord,
    room_name: str,
    address: str,
    city: str,
    group_name: str = "",
    province: str = "bc",
    asking_rent: str = "",
    inventory_items: str = "",
    start_date: str = "",
    end_date: str = "",
    total_rent: str = "",
    security_deposit: str = "",
    pet_deposit: str = "0",
    cleaning_fee: str = "0",
    special_terms: str = "",
    tenant_name: str = "",
    tenant_email: str = "",
    smoking_allowed: str = "0",
    pets_allowed: str = "0",
    create_inspection: str = "1",
    use_existing_if_name_matches: str = "1",
    confirm: str = "",
) -> dict:
    """ONE-SHOT workflow: create/reuse room + inventory + DRAFT lease + invite +
    condition inspection. Use when landlord asks for a full room setup with tenant.
    Prefer over many separate tools. Preview; confirm=yes runs all steps."""
    from .domain_crud import setup_room_tenancy as _fn

    return _fn(
        landlord,
        room_name=room_name,
        address=address,
        city=city,
        group_name=group_name,
        province=province,
        asking_rent=asking_rent,
        inventory_items=inventory_items,
        start_date=start_date,
        end_date=end_date,
        total_rent=total_rent,
        security_deposit=security_deposit,
        pet_deposit=pet_deposit,
        cleaning_fee=cleaning_fee,
        special_terms=special_terms,
        tenant_name=tenant_name,
        tenant_email=tenant_email,
        smoking_allowed=smoking_allowed,
        pets_allowed=pets_allowed,
        create_inspection=create_inspection,
        use_existing_if_name_matches=use_existing_if_name_matches,
        confirm=confirm,
    )


def update_property(
    landlord,
    property_query: str,
    name: str = "",
    status: str = "",
    description: str = "",
    address: str = "",
    city: str = "",
    province: str = "",
    asking_rent: str = "",
    unit_type: str = "",
    room_type: str = "",
    is_publicly_visible: str = "",
    confirm: str = "",
) -> dict:
    """Update listing fields. status: AVAILABLE|OCCUPIED|MAINTENANCE|NOT_AVAILABLE.
    Preview; confirm=yes."""
    from .domain_crud import update_property as _fn
    return _fn(
        landlord, property_query=property_query, name=name, status=status,
        description=description, address=address, city=city, province=province,
        asking_rent=asking_rent, unit_type=unit_type, room_type=room_type,
        is_publicly_visible=is_publicly_visible, confirm=confirm,
    )


def delete_property(
    landlord, property_query: str, pick: str = "", confirm: str = ""
) -> dict:
    """Delete a listing. Blocked if ANY lease still references it (PROTECT).
    On duplicate names pass property_query=<id> or pick=first|no_group|with_group|2.
    Preview; confirm=yes."""
    from .domain_crud import delete_property as _fn
    return _fn(landlord, property_query=property_query, pick=pick, confirm=confirm)


def create_property_group(
    landlord, name: str, description: str = "", confirm: str = ""
) -> dict:
    """Create a property group for shared rooms (e.g. McKenzie Side Unit). Preview; confirm=yes."""
    from .domain_crud import create_property_group as _fn
    return _fn(landlord, name=name, description=description, confirm=confirm)


def assign_property_to_group(
    landlord,
    property_query: str,
    group_name: str = "",
    clear: str = "",
    confirm: str = "",
) -> dict:
    """Put a ROOM into a group, or clear=yes to remove from group. Complete units blocked.
    Preview; confirm=yes."""
    from .domain_crud import assign_property_to_group as _fn
    return _fn(
        landlord, property_query=property_query, group_name=group_name,
        clear=clear, confirm=confirm,
    )


def create_lease(
    landlord,
    property_query: str,
    start_date: str,
    end_date: str = "",
    total_rent: str = "0",
    security_deposit: str = "",
    pet_deposit: str = "0",
    cleaning_fee: str = "0",
    is_month_to_month: str = "0",
    pets_allowed: str = "0",
    smoking_allowed: str = "0",
    special_terms: str = "",
    etransfer_email: str = "",
    bills_included: str = "",
    pick: str = "",
    confirm: str = "",
) -> dict:
    """Create a DRAFT lease (UI New Lease). Type auto: room→Standard Roommate,
    BC complete unit→RTB-1. Fixed-term needs end_date.
    property_query can be listing id or name; pick=first|with_group|… if duplicates.
    Defaults for protection: no smoking/pets; pet_deposit & cleaning_fee 0 unless set.
    security_deposit: omit for half monthly rent; pass '0' only if landlord wants zero.
    Then invite tenants; use create_condition_inspection (not schedule_viewing) for move-in.
    Preview; confirm=yes."""
    from .domain_crud import create_lease as _fn
    return _fn(
        landlord, property_query=property_query, start_date=start_date,
        end_date=end_date, total_rent=total_rent, security_deposit=security_deposit,
        pet_deposit=pet_deposit, cleaning_fee=cleaning_fee,
        is_month_to_month=is_month_to_month, pets_allowed=pets_allowed,
        smoking_allowed=smoking_allowed, special_terms=special_terms,
        etransfer_email=etransfer_email, bills_included=bills_included,
        pick=pick, confirm=confirm,
    )


def update_lease(
    landlord,
    property_query: str = "",
    lease_number: str = "",
    total_rent: str = "",
    security_deposit: str = "",
    start_date: str = "",
    end_date: str = "",
    pets_allowed: str = "",
    smoking_allowed: str = "",
    special_terms: str = "",
    etransfer_email: str = "",
    is_month_to_month: str = "",
    confirm: str = "",
) -> dict:
    """Update draft/pending lease fields. Blocked if ACTIVE/locked (LeaseNotLocked).
    Changing total_rent rebalances unsigned shares. Preview; confirm=yes."""
    from .domain_crud import update_lease as _fn
    return _fn(
        landlord, property_query=property_query, lease_number=lease_number,
        total_rent=total_rent, security_deposit=security_deposit,
        start_date=start_date, end_date=end_date, pets_allowed=pets_allowed,
        smoking_allowed=smoking_allowed, special_terms=special_terms,
        etransfer_email=etransfer_email, is_month_to_month=is_month_to_month,
        confirm=confirm,
    )


def delete_draft_lease(
    landlord, property_query: str = "", lease_number: str = "", confirm: str = ""
) -> dict:
    """Delete ONLY a DRAFT lease. Pending/active → terminate_lease. Preview; confirm=yes."""
    from .domain_crud import delete_draft_lease as _fn
    return _fn(
        landlord, property_query=property_query, lease_number=lease_number, confirm=confirm
    )


def terminate_lease(
    landlord,
    property_query: str = "",
    lease_number: str = "",
    termination_date: str = "",
    move_out_date: str = "",
    confirm: str = "",
) -> dict:
    """Terminate pending/active lease (not draft). Voids open charges, closes occupancy.
    Same as UI Terminate. Preview; confirm=yes."""
    from .domain_crud import terminate_lease as _fn
    return _fn(
        landlord, property_query=property_query, lease_number=lease_number,
        termination_date=termination_date, move_out_date=move_out_date, confirm=confirm,
    )


def landlord_sign_lease(
    landlord, property_query: str = "", lease_number: str = "", confirm: str = ""
) -> dict:
    """Record landlord signature. Rent must be fully allocated across tenants first.
    May activate lease if a tenant already signed. Preview; confirm=yes."""
    from .domain_crud import landlord_sign_lease as _fn
    return _fn(
        landlord, property_query=property_query, lease_number=lease_number, confirm=confirm
    )


def update_work_order(
    landlord,
    work_order_id: str = "",
    title_query: str = "",
    title: str = "",
    description: str = "",
    priority: str = "",
    category: str = "",
    contractor_name: str = "",
    contractor_phone: str = "",
    scheduled_date: str = "",
    confirm: str = "",
) -> dict:
    """Update work order fields (not status). Status → transition_work_order or
    complete_work_order. Never delete WOs — cancel via transition. Preview; confirm=yes."""
    from .domain_crud import update_work_order as _fn
    return _fn(
        landlord, work_order_id=work_order_id, title_query=title_query, title=title,
        description=description, priority=priority, category=category,
        contractor_name=contractor_name, contractor_phone=contractor_phone,
        scheduled_date=scheduled_date, confirm=confirm,
    )


def complete_work_order(
    landlord,
    work_order_id: str = "",
    title_query: str = "",
    cost: str = "",
    post_expense: str = "0",
    vendor: str = "",
    confirm: str = "",
) -> dict:
    """Complete a work order (FSM). Optional cost; post_expense=yes books MAINTENANCE
    ledger entry (same as UI complete). Preview; confirm=yes."""
    from .domain_crud import complete_work_order as _fn
    return _fn(
        landlord, work_order_id=work_order_id, title_query=title_query, cost=cost,
        post_expense=post_expense, vendor=vendor, confirm=confirm,
    )


def add_work_order_comment(
    landlord,
    body: str,
    work_order_id: str = "",
    title_query: str = "",
    confirm: str = "",
) -> dict:
    """Add a comment on a work order. Preview; confirm=yes."""
    from .domain_crud import add_work_order_comment as _fn
    return _fn(
        landlord, body=body, work_order_id=work_order_id, title_query=title_query,
        confirm=confirm,
    )


def create_inventory_item(
    landlord,
    property_query: str,
    name: str,
    quantity: str = "1",
    condition: str = "GOOD",
    location: str = "",
    description: str = "",
    confirm: str = "",
) -> dict:
    """Add private inventory/furniture to a listing. Preview; confirm=yes."""
    from .domain_crud import create_inventory_item as _fn
    return _fn(
        landlord, property_query=property_query, name=name, quantity=quantity,
        condition=condition, location=location, description=description, confirm=confirm,
    )


def update_inventory_item(
    landlord,
    property_query: str,
    item_name: str,
    name: str = "",
    quantity: str = "",
    condition: str = "",
    location: str = "",
    description: str = "",
    confirm: str = "",
) -> dict:
    """Update a private inventory item by name match. Preview; confirm=yes."""
    from .domain_crud import update_inventory_item as _fn
    return _fn(
        landlord, property_query=property_query, item_name=item_name, name=name,
        quantity=quantity, condition=condition, location=location,
        description=description, confirm=confirm,
    )


def delete_inventory_item(
    landlord, property_query: str, item_name: str, confirm: str = ""
) -> dict:
    """Delete private inventory item. Preview; confirm=yes."""
    from .domain_crud import delete_inventory_item as _fn
    return _fn(
        landlord, property_query=property_query, item_name=item_name, confirm=confirm
    )


def create_shared_inventory_item(
    landlord,
    group_name: str,
    name: str,
    quantity: str = "1",
    condition: str = "GOOD",
    location: str = "",
    description: str = "",
    confirm: str = "",
) -> dict:
    """Add shared inventory to a property group (e.g. kitchen). Preview; confirm=yes."""
    from .domain_crud import create_shared_inventory_item as _fn
    return _fn(
        landlord, group_name=group_name, name=name, quantity=quantity,
        condition=condition, location=location, description=description, confirm=confirm,
    )


def delete_shared_inventory_item(
    landlord, group_name: str, item_name: str, confirm: str = ""
) -> dict:
    """Delete shared inventory item. Preview; confirm=yes."""
    from .domain_crud import delete_shared_inventory_item as _fn
    return _fn(
        landlord, group_name=group_name, item_name=item_name, confirm=confirm
    )


def crud_capabilities(landlord) -> dict:
    """List what CRUD the agent can do and UI restrictions (read-only help).
    Call when unsure which write tool to use or what is forbidden."""
    from .domain_crud import crud_capabilities as _fn
    return _fn(landlord)
