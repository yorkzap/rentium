"""Shared period projections for the UI and RAMA. Dates use [start, end)."""

from datetime import date
from decimal import Decimal

from django.db.models import Q, Sum
from django.utils import timezone

from .models import (
    BankTransaction,
    EntryType,
    ExpenseCategory,
    INCOME_CHARGE_TYPES,
    LedgerEntry,
)
from .position import Scope, financial_position
from .services import LedgerError


def money(value):
    return str((value or Decimal("0")).quantize(Decimal("0.01")))


def total(rows):
    return rows.aggregate(value=Sum("amount"))["value"] or Decimal("0.00")


def scoped_entries(landlord, *, property_id=None, holding_id=None, unit_id=None):
    from rentium.properties.models import Property, PropertyHolding, PropertyUnit

    rows = LedgerEntry.objects.filter(landlord=landlord).not_voided()
    for ident, model in (
        (property_id, Property),
        (holding_id, PropertyHolding),
        (unit_id, PropertyUnit),
    ):
        if ident and not model.objects.filter(pk=ident, landlord=landlord).exists():
            raise LedgerError("That financial scope is not in this portfolio.")
    direct, settlement = Q(), Q()
    if property_id:
        direct &= Q(property_id=property_id)
        settlement &= Q(settles__property_id=property_id)
    if holding_id:
        direct &= Q(holding_id=holding_id) | Q(property__holding_id=holding_id)
        settlement &= Q(settles__holding_id=holding_id) | Q(
            settles__property__holding_id=holding_id
        )
    if unit_id:
        direct &= Q(property__unit_id=unit_id)
        settlement &= Q(settles__property__unit_id=unit_id)
    return rows.filter(
        (~Q(entry_type__in=[EntryType.PAYMENT, EntryType.CREDIT]) & direct)
        | (Q(entry_type__in=[EntryType.PAYMENT, EntryType.CREDIT]) & settlement)
    )


def period_money(landlord, start, end, **scope):
    """Compatibility fields remain incurred-based; cash fields are explicit."""
    live = scoped_entries(landlord, **scope)
    received = live.filter(
        entry_type=EntryType.PAYMENT, effective_date__gte=start, effective_date__lt=end
    )
    income = total(received.filter(settles__entry_type__in=INCOME_CHARGE_TYPES))
    deposits = total(received.filter(settles__entry_type=EntryType.DEPOSIT_CHARGE))
    incurred = live.filter(
        entry_type=EntryType.EXPENSE, effective_date__gte=start, effective_date__lt=end
    )
    cleared = live.filter(
        entry_type=EntryType.EXPENSE, paid_on__gte=start, paid_on__lt=end
    )
    returns = total(
        live.filter(
            entry_type=EntryType.DEPOSIT_RETURN,
            effective_date__gte=start,
            effective_date__lt=end,
        )
    )
    spent, cash_out = total(incurred), total(cleared)
    return {
        "month": start.strftime("%Y-%m"),
        "label": start.strftime("%b %Y"),
        "expected_income": money(
            total(live.expected_income().filter(due_date__gte=start, due_date__lt=end))
        ),
        "collected_income": money(income),
        "expenses": money(spent),
        "net": money(income - spent),
        "deposits_collected": money(deposits),
        "damage_claimed": money(
            total(live.damage_claims().filter(due_date__gte=start, due_date__lt=end))
        ),
        "expenses_cleared": money(cash_out),
        "mortgage_cleared": money(
            total(cleared.filter(category=ExpenseCategory.MORTGAGE))
        ),
        "deposits_returned": money(returns),
        "recorded_cash_in": money(income + deposits),
        "recorded_cash_out": money(cash_out + returns),
        "recorded_cash_movement": money(income + deposits - cash_out - returns),
        "basis": "Payments received and expenses cleared; deposits included in cash, excluded from income.",
    }


def entry_row(entry):
    return {
        "id": str(entry.pk),
        "description": entry.description,
        "amount": money(entry.amount),
        "entry_type": entry.entry_type,
        "category": entry.category,
        "effective_date": str(entry.effective_date),
        "paid_on": str(entry.paid_on) if entry.paid_on else None,
        "due_date": str(entry.due_date) if entry.due_date else None,
        "property": str(entry.property_id) if entry.property_id else None,
        "holding": str(entry.holding_id) if entry.holding_id else None,
        "scope_name": entry.property.name
        if entry.property_id
        else entry.holding.name
        if entry.holding_id
        else "Portfolio",
        "outstanding": money(entry.outstanding)
        if hasattr(entry, "outstanding")
        else None,
        "link": f"/dashboard/financial?entry={entry.pk}",
    }


def overview(landlord, start, end, **scope):
    if (
        not isinstance(start, date)
        or not isinstance(end, date)
        or start >= end
        or (end - start).days > 732
    ):
        raise LedgerError("Choose a valid period of no more than two years.")
    live = scoped_entries(landlord, **scope)
    today = timezone.localdate()
    charges = live.charges().with_settlement().select_related("property", "holding")
    rent = charges.expected_income().filter(due_date__gte=start, due_date__lt=end)
    overdue = (
        charges.open_charges(as_of=today).expected_income().filter(due_date__lt=today)
    )
    bills = live.filter(entry_type=EntryType.EXPENSE, paid_on__isnull=True)
    deposits = live.filter(
        entry_type=EntryType.PAYMENT, settles__entry_type=EntryType.DEPOSIT_CHARGE
    )
    held = total(deposits) - total(live.filter(entry_type=EntryType.DEPOSIT_RETURN))
    # Scope the all-time liability through the same charge predicate as every
    # existing position reader. A combined filter is handled by live above.
    if not any(scope.values()):
        held = financial_position(landlord, scope=Scope.portfolio()).deposits_held
    cash = live.filter(
        Q(
            entry_type__in=[EntryType.PAYMENT, EntryType.DEPOSIT_RETURN],
            effective_date__gte=start,
            effective_date__lt=end,
        )
        | Q(entry_type=EntryType.EXPENSE, paid_on__gte=start, paid_on__lt=end)
    )
    pending = BankTransaction.objects.filter(
        statement__batch__landlord=landlord, result={}
    ).count()
    return {
        "start": str(start),
        "end_exclusive": str(end),
        "as_of": timezone.now().isoformat(),
        "scope": scope,
        "period": period_money(landlord, start, end, **scope),
        "rent_overdue": money(overdue.aggregate(s=Sum("outstanding"))["s"]),
        "deposits_held": money(held),
        "unpaid_bills": money(total(bills)),
        "coverage": {
            "status": "RECORDED_ONLY",
            "unresolved_bank_rows_portfolio": pending,
            "message": "Recorded activity, not a bank balance. Unreviewed statements and missing records are not included.",
        },
        "rent": [entry_row(e) for e in rent.order_by("due_date", "id")],
        "overdue": [entry_row(e) for e in overdue.order_by("due_date", "id")],
        "bills": [
            entry_row(e)
            for e in bills.select_related("property", "holding").order_by(
                "effective_date", "id"
            )
        ],
        "cash": [
            entry_row(e)
            for e in cash.select_related("property", "holding").order_by(
                "effective_date", "id"
            )
        ],
    }
