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
    """Whole-portfolio state of the union: property and lease counts, this
    month's expected vs collected income and expenses, outstanding and
    overdue totals, deposits held, the next upcoming charge, open work
    orders, and the top items needing attention. Call this first for any
    broad "how are things going" question."""
    from .union import state_of_the_union

    return state_of_the_union(landlord)


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
    """Full state of one lease: status, dates, monthly rent, deposits, and
    who is on it. Get the lease_id from resolve_person or attention_items
    first."""
    from django.core.exceptions import ValidationError

    from rentium.leases.models import Lease

    try:
        lease = (
            Lease.objects.select_related("property", "group")
            .prefetch_related("lease_tenants__tenant__user")
            .get(pk=lease_id, landlord=landlord)
        )
    except (Lease.DoesNotExist, ValidationError, ValueError):
        return {"error": f"No lease {lease_id!r} in this portfolio."}

    place = (
        lease.property.name
        if lease.property
        else (lease.group.name if lease.group else "")
    )
    return {
        "lease_id": str(lease.pk),
        "status": lease.status,
        "property": place,
        "start_date": lease.start_date.isoformat(),
        "end_date": lease.end_date.isoformat() if lease.end_date else None,
        "is_month_to_month": lease.is_month_to_month,
        "monthly_rent": str(lease.get_total_monthly_rent()),
        "security_deposit": str(lease.security_deposit),
        "pet_deposit": str(lease.pet_deposit),
        "tenants": [
            {
                "name": lt.display_name,
                "is_primary": lt.is_primary_tenant,
                "has_signed": lt.has_signed,
                "declined": lt.declined,
            }
            for lt in lease.lease_tenants.all()
        ],
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
    """Money for one month (format 2026-07; empty = current month): expected
    vs collected income, expenses, net, and deposits collected, across the
    whole portfolio."""
    from .union import month_money as compute

    try:
        start, end = _month_bounds(month)
    except ValueError as exc:
        return {"error": str(exc)}
    return compute(landlord, start, end)


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
    """Maintenance work orders that are not completed or cancelled, most
    urgent first, with their SLA deadlines."""
    from rentium.maintenance.models import WorkOrder

    orders = (
        WorkOrder.objects.filter(property__landlord=landlord)
        .exclude(status__in=[WorkOrder.Status.COMPLETED, WorkOrder.Status.CANCELLED])
        .select_related("property")
        .order_by("sla_due_at")[:20]
    )
    return {
        "work_orders": [
            {
                "title": order.title,
                "property": order.property.name,
                "status": order.status,
                "priority": order.priority,
                "sla_due_at": order.sla_due_at.isoformat() if order.sla_due_at else None,
            }
            for order in orders
        ]
    }
