"""
Recording what a property cost, is worth, and owes.

These are the WRITE side of the financial data layer, and they belong to the
General — not the Treasurer. The finance head reads these numbers and reasons
about them; putting the pen in its hand as well would mean the same agent that
concludes "your equity looks strong" can also adjust the valuation that
conclusion rests on.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal, InvalidOperation

from .domain_crud import _confirmed, _preview, _resolve_holding


def _amount(raw, field: str):
    """(Decimal|None, error|None) — blank is a legitimate 'not stated'."""
    text = str(raw or "").replace("$", "").replace(",", "").strip()
    if not text:
        return None, None
    try:
        value = Decimal(text)
    except (InvalidOperation, ValueError):
        return None, {"error": f"{field} must be a number, got {raw!r}."}
    if value < 0:
        return None, {"error": f"{field} cannot be negative."}
    return value, None


def _day(raw, field: str):
    text = str(raw or "").strip()[:10]
    if not text:
        return None, None
    try:
        return date.fromisoformat(text), None
    except ValueError:
        return None, {"error": f"{field} must be YYYY-MM-DD, got {raw!r}."}


def record_holding_financials(
    landlord,
    *,
    holding_name: str,
    purchase_price: str = "",
    purchase_date: str = "",
    year_built: str = "",
    heating_type: str = "",
    capital_improvements: str = "",
    confirm: str = "",
) -> dict:
    from rentium.ledger.models import HoldingFinancials

    holding, err = _resolve_holding(landlord, holding_name)
    if err:
        return {"error": err}

    price, e = _amount(purchase_price, "purchase_price")
    if e:
        return e
    bought, e = _day(purchase_date, "purchase_date")
    if e:
        return e
    improvements, e = _amount(capital_improvements, "capital_improvements")
    if e:
        return e
    built = None
    if str(year_built or "").strip():
        try:
            built = int(str(year_built).strip())
        except ValueError:
            return {"error": f"year_built must be a year, got {year_built!r}."}

    changes = {
        k: v
        for k, v in {
            "purchase_price": price,
            "purchase_date": bought,
            "capital_improvements_to_date": improvements,
            "year_built": built,
            "heating_type": (heating_type or "").strip(),
        }.items()
        if v not in (None, "")
    }
    if not changes:
        return {"error": "Nothing to record — give at least one detail."}

    preview = {"holding": holding.name, "changes": {k: str(v) for k, v in changes.items()}}
    if not _confirmed(confirm):
        return _preview(
            "record_holding_financials", preview, "Records acquisition details."
        )

    row, _created = HoldingFinancials.objects.get_or_create(
        holding=holding, defaults={"landlord": landlord}
    )
    for field, value in changes.items():
        setattr(row, field, value)
    row.save()
    return {"recorded": True, "holding": holding.name, "changes": preview["changes"]}


def record_valuation(
    landlord,
    *,
    holding_name: str,
    amount: str,
    as_of: str = "",
    basis: str = "LANDLORD_ESTIMATE",
    confirm: str = "",
) -> dict:
    """Add a valuation. Never replaces an earlier one — the series is the point."""
    from rentium.ledger.models import HoldingValuation

    holding, err = _resolve_holding(landlord, holding_name)
    if err:
        return {"error": err}
    value, e = _amount(amount, "amount")
    if e:
        return e
    if value is None:
        return {"error": "amount is required."}
    day, e = _day(as_of, "as_of")
    if e:
        return e
    day = day or date.today()

    chosen = (basis or "").strip().upper() or "LANDLORD_ESTIMATE"
    if chosen not in HoldingValuation.Basis.values:
        return {
            "error": f"basis must be one of {sorted(HoldingValuation.Basis.values)}."
        }

    existing = HoldingValuation.objects.filter(
        holding=holding, as_of=day, basis=chosen
    ).first()
    preview = {
        "holding": holding.name,
        "amount": str(value),
        "as_of": day.isoformat(),
        "basis": chosen,
        "replaces": str(existing.amount) if existing else "",
    }
    if not _confirmed(confirm):
        return _preview(
            "record_valuation",
            preview,
            "Corrects the figure already recorded on that date and basis."
            if existing
            else "Adds a valuation to this property's history.",
        )

    row, _created = HoldingValuation.objects.update_or_create(
        holding=holding,
        as_of=day,
        basis=chosen,
        defaults={"landlord": landlord, "amount": value},
    )
    return {
        "recorded": True,
        "holding": holding.name,
        "valuation": {"amount": str(row.amount), "as_of": row.as_of.isoformat()},
    }


def record_mortgage(
    landlord,
    *,
    holding_name: str,
    current_principal: str = "",
    balance_as_of: str = "",
    rate_percent: str = "",
    payment_amount: str = "",
    payment_frequency: str = "MONTHLY",
    term_end: str = "",
    lender: str = "",
    confirm: str = "",
) -> dict:
    """Record the mortgage. An existing one is superseded, never edited, so
    "what rate were we on in 2024" stays answerable at renewal time."""
    from rentium.ledger.models import HoldingMortgage

    holding, err = _resolve_holding(landlord, holding_name)
    if err:
        return {"error": err}

    principal, e = _amount(current_principal, "current_principal")
    if e:
        return e
    payment, e = _amount(payment_amount, "payment_amount")
    if e:
        return e
    rate, e = _amount(rate_percent, "rate_percent")
    if e:
        return e
    if rate is not None and rate > 30:
        return {"error": f"rate_percent {rate} looks like basis points, not a rate."}
    known_on, e = _day(balance_as_of, "balance_as_of")
    if e:
        return e
    ends, e = _day(term_end, "term_end")
    if e:
        return e
    if principal is not None and known_on is None:
        return {
            "error": (
                "A balance needs the date it was true on (balance_as_of) — "
                "without it I can't tell how stale it is."
            )
        }

    current = holding.mortgages.filter(status=HoldingMortgage.Status.ACTIVE).first()
    preview = {
        "holding": holding.name,
        "lender": (lender or "").strip(),
        "current_principal": str(principal or ""),
        "balance_as_of": known_on.isoformat() if known_on else "",
        "rate_percent": str(rate or ""),
        "payment_amount": str(payment or ""),
        "term_end": ends.isoformat() if ends else "",
        "supersedes": (
            f"{current.lender or 'existing mortgage'} at {current.rate_percent}%"
            if current
            else ""
        ),
    }
    if not _confirmed(confirm):
        return _preview(
            "record_mortgage",
            preview,
            "Replaces the current mortgage (the old one is kept as history)."
            if current
            else "Records the mortgage on this property.",
        )

    if current is not None:
        current.status = HoldingMortgage.Status.SUPERSEDED
        current.save(update_fields=["status", "updated_at"])
    row = HoldingMortgage.objects.create(
        landlord=landlord,
        holding=holding,
        lender=(lender or "").strip(),
        current_principal=principal,
        current_principal_as_of=known_on,
        rate_percent=rate,
        payment_amount=payment,
        payment_frequency=(payment_frequency or "MONTHLY").strip().upper(),
        term_end=ends,
        supersedes=current,
    )
    return {
        "recorded": True,
        "holding": holding.name,
        "mortgage": {"id": str(row.pk), "superseded": bool(current)},
    }
