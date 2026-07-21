"""
Bank balances — landlord-reported, per holding (house/building) or
portfolio-wide. NOT a bank feed: this is what the landlord tells RAMA their
account holds, as of a date. RAMA may only update it with the landlord's
explicit confirmation (own_confirm in tool_meta) — a wrong "auto-corrected"
balance is worse than a stale one.

`ledger_drift_since` is the honesty check: how much the ledger says should
have moved since the balance was last reported, so a Sergeant can tell
"the balance dropped below the rule" from "this number is just old."
"""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

from django.db.models import Q, Sum

STALE_AFTER_DAYS = 14


def _properties_for_holding(landlord, holding):
    from rentium.properties.models import Property

    qs = Property.objects.filter(landlord=landlord)
    return qs.filter(holding=holding) if holding is not None else qs


def ledger_drift_since(landlord, holding, since: date) -> Decimal:
    """Net ledger movement (money in − money out) for this holding's
    properties since `since`. Positive = balance likely grew; negative =
    likely shrank. Portfolio-wide (holding=None) sums everything.

    Money-in: settled PAYMENT/CREDIT effective_date > since.
    Money-out: EXPENSE with paid_on > since (only what's actually left the
    bank — unpaid/unsettled expenses haven't moved anything yet).
    """
    from rentium.ledger.models import EntryType, LedgerEntry

    props = _properties_for_holding(landlord, holding)
    prop_filter = Q(property__in=props) if holding is not None else Q(landlord=landlord)

    inflow = (
        LedgerEntry.objects.filter(
            prop_filter,
            landlord=landlord,
            entry_type__in=[EntryType.PAYMENT, EntryType.CREDIT],
            effective_date__gt=since,
        ).aggregate(total=Sum("amount"))["total"]
        or Decimal("0")
    )
    outflow = (
        LedgerEntry.objects.filter(
            prop_filter,
            landlord=landlord,
            entry_type=EntryType.EXPENSE,
            paid_on__gt=since,
        ).aggregate(total=Sum("amount"))["total"]
        or Decimal("0")
    )
    return inflow - outflow


def balance_payload(row) -> dict:
    stale = (date.today() - row.as_of) > timedelta(days=STALE_AFTER_DAYS)
    return {
        "id": row.pk,
        "holding": row.holding.name if row.holding_id else None,
        "holding_id": str(row.holding_id) if row.holding_id else None,
        "label": row.label,
        "balance": str(row.balance),
        "as_of": str(row.as_of),
        "updated_via": row.updated_via,
        "stale": stale,
        "estimated_drift_since_reported": str(
            ledger_drift_since(row.landlord, row.holding_id and row.holding, row.as_of)
        ),
    }


def update_bank_balance(
    landlord, *, holding_name: str = "", label: str = "Operating",
    balance: str = "", as_of: str = "", confirm: str = "",
) -> dict:
    """Record the landlord's reported balance for one holding (or the whole
    portfolio if holding_name is blank). Always requires the landlord's
    explicit confirmation — RAMA never silently changes what it thinks a
    bank account holds."""
    from datetime import datetime as _dt

    from .domain_crud import _confirmed, _money, _preview, _resolve_holding

    holding = None
    if (holding_name or "").strip():
        holding, err = _resolve_holding(landlord, holding_name)
        if err:
            return {"error": err}

    try:
        amount = _money(balance)
    except ValueError as exc:
        return {"error": str(exc)}

    as_of_s = (as_of or "").strip()
    try:
        as_of_date = (
            _dt.strptime(as_of_s[:10], "%Y-%m-%d").date() if as_of_s else date.today()
        )
    except ValueError:
        return {"error": f"Invalid as_of {as_of!r}; use YYYY-MM-DD."}

    preview = {
        "holding": holding.name if holding else "(portfolio-wide)",
        "label": label,
        "balance": str(amount),
        "as_of": str(as_of_date),
    }
    if not _confirmed(confirm):
        return _preview(
            "update_bank_balance", preview,
            "Records the landlord's reported bank balance.",
        )

    from rentium.ledger.models import PropertyBankBalance

    row, _created = PropertyBankBalance.objects.update_or_create(
        landlord=landlord, holding=holding,
        defaults={
            "label": (label or "Operating")[:100],
            "balance": amount,
            "as_of": as_of_date,
            "updated_via": PropertyBankBalance.Source.CHAT,
        },
    )
    return {"updated": True, "balance": balance_payload(row)}


def list_bank_balances(landlord) -> dict:
    """Landlord-reported bank balances per holding (+ portfolio-wide if
    set), with staleness and estimated ledger drift since last reported."""
    from rentium.ledger.models import PropertyBankBalance

    rows = PropertyBankBalance.objects.filter(landlord=landlord).select_related(
        "holding"
    )
    return {
        "balances": [balance_payload(r) for r in rows],
        "count": rows.count(),
        "instruction": (
            f"'stale' means reported more than {STALE_AFTER_DAYS} days ago — "
            "ask the landlord to re-confirm before treating it as current. "
            "estimated_drift_since_reported is ledger movement since as_of, "
            "not a live balance."
        ),
    }
