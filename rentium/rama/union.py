"""
"State of the Union": a service-layer aggregate of the whole portfolio —
money this month, outstanding/overdue, deposits held, open work, and what
needs attention. Built exactly like ledger's summary_view and useful on the
dashboard before any AI touches it — which is the test every RAMA component
must pass: useful without the model, safer with it.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from django.db.models import Count, Sum


def _month_bounds(day: date) -> tuple[date, date]:
    start = day.replace(day=1)
    end = date(start.year + start.month // 12, start.month % 12 + 1, 1)
    return start, end


def month_money(landlord, start: date, end: date) -> dict:
    """Expected vs collected income, expenses, and net for [start, end).

    Same queryset math as ledger's summary_view, for a single month —
    deposits stay out of income (refundable liability) but are reported.
    """
    from rentium.ledger import services
    from rentium.ledger.models import INCOME_CHARGE_TYPES, EntryType, LedgerEntry

    live = LedgerEntry.objects.filter(landlord=landlord).not_voided()

    expected = live.filter(
        entry_type__in=INCOME_CHARGE_TYPES, due_date__gte=start, due_date__lt=end
    ).aggregate(s=Sum("amount"))["s"] or Decimal("0.00")

    collected = live.filter(
        entry_type=EntryType.PAYMENT,
        settles__entry_type__in=INCOME_CHARGE_TYPES,
        effective_date__gte=start,
        effective_date__lt=end,
    ).aggregate(s=Sum("amount"))["s"] or Decimal("0.00")

    spent = live.filter(
        entry_type=EntryType.EXPENSE,
        effective_date__gte=start,
        effective_date__lt=end,
    ).aggregate(s=Sum("amount"))["s"] or Decimal("0.00")

    deposits_in = services.deposits_collected_between(landlord, start, end)

    return {
        "month": start.strftime("%Y-%m"),
        "expected_income": str(expected),
        "collected_income": str(collected),
        "expenses": str(spent),
        "net": str(collected - spent),
        "deposits_collected": str(deposits_in),
    }


def state_of_the_union(landlord) -> dict:
    from rentium.attention.service import compute_attention
    from rentium.leases.models import Lease
    from rentium.ledger import services
    from rentium.ledger.models import INCOME_CHARGE_TYPES, LedgerEntry
    from rentium.maintenance.models import WorkOrder
    from rentium.properties.models import Property

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

    return {
        "as_of": today.isoformat(),
        "portfolio": {
            "properties": Property.objects.filter(landlord=landlord).count(),
            "leases": lease_counts,
        },
        "this_month": month_money(landlord, start, end),
        "outstanding": {
            "total": str(agg["total"] or Decimal("0.00")),
            "count": agg["count"] or 0,
            "overdue_count": overdue_count,
        },
        "deposits_held": str(services.deposits_held(landlord)),
        "next_charge": services.next_upcoming_charge(landlord),
        "open_work_orders": open_work,
        "attention": {
            "counts": severity_counts,
            "top": [item.as_dict() for item in items[:5]],
        },
    }
