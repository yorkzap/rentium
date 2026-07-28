"""
Mortgage arithmetic, in Python, returning Figures.

None of this is model work. Amortization is a closed-form calculation with one
right answer, and a language model asked to compute a balance will produce a
plausible one. Everything here is deterministic; the Treasurer's job is to
decide which of these numbers matters and say so, not to compute them.

Every return is a `Figure`, so a projected balance arrives already labelled as
projected and carrying the date it was projected FROM. That matters more than
it sounds: equity is a valuation minus a balance, and those two are almost
never measured on the same day.
"""

from __future__ import annotations

from datetime import date
from decimal import ROUND_HALF_UP, Decimal

from .render import Figure, Provenance, SourceType, unknown

CENTS = Decimal("0.01")

_PAYMENTS_PER_YEAR = {
    "MONTHLY": 12,
    "SEMI_MONTHLY": 24,
    "BIWEEKLY": 26,
    "WEEKLY": 52,
    "ACCELERATED_BIWEEKLY": 26,
    "ACCELERATED_WEEKLY": 52,
}


def _money(value: Decimal) -> Decimal:
    return Decimal(value).quantize(CENTS, rounding=ROUND_HALF_UP)


def payments_per_year(mortgage) -> int:
    return _PAYMENTS_PER_YEAR.get(
        (mortgage.payment_frequency or "").strip().upper(), 12
    )


def _periodic_rate(mortgage) -> Decimal | None:
    """Canadian fixed mortgages compound SEMI-ANNUALLY, not monthly.

    Using the US monthly-compounding formula here would overstate interest on
    every Canadian mortgage in the system — small per payment, thousands over a
    term. Worth the extra line.
    """
    if mortgage.rate_percent is None:
        return None
    annual = Decimal(mortgage.rate_percent) / Decimal("100")
    if annual == 0:
        return Decimal("0")
    n = payments_per_year(mortgage)
    semi_annual = annual / Decimal("2")
    # (1 + i/2)^(2/n) - 1
    effective = (Decimal("1") + semi_annual) ** (Decimal("2") / Decimal(n))
    return effective - Decimal("1")


def principal_at(mortgage, on: date) -> Figure:
    """The balance owing on a date, projected from the last known figure.

    Projection is the honest word: the landlord told us a balance on some date
    and we roll it forward at the contracted payment. It is not a statement
    balance and never claims to be.
    """
    if mortgage.current_principal is None or mortgage.current_principal_as_of is None:
        return unknown(
            "mortgage balance",
            note="no balance and as-of date on file",
        )
    known_on = mortgage.current_principal_as_of
    balance = Decimal(mortgage.current_principal)
    if on <= known_on:
        return Figure(
            value=_money(balance),
            label="mortgage balance",
            provenance=Provenance(
                source_type=SourceType.LANDLORD,
                as_of=known_on,
                ref=str(mortgage.pk),
            ),
        )

    rate = _periodic_rate(mortgage)
    payment = mortgage.payment_amount
    if rate is None or payment is None:
        # We know the balance but cannot roll it forward. Report what we know
        # with its real date rather than inventing a current figure.
        return Figure(
            value=_money(balance),
            label="mortgage balance",
            provenance=Provenance(
                source_type=SourceType.LANDLORD,
                as_of=known_on,
                ref=str(mortgage.pk),
                note="not projected — rate or payment missing",
            ),
        )

    payment = Decimal(payment)
    periods = int(
        (on - known_on).days / (Decimal("365.25") / Decimal(payments_per_year(mortgage)))
    )
    for _ in range(max(periods, 0)):
        interest = balance * rate
        balance = balance + interest - payment
        if balance <= 0:
            balance = Decimal("0")
            break
    return Figure(
        value=_money(balance),
        label="mortgage balance",
        provenance=Provenance(
            source_type=SourceType.ESTIMATE,
            as_of=on,
            ref=str(mortgage.pk),
            note=f"projected from {known_on.isoformat()}",
        ),
    )


def interest_paid_between(mortgage, start: date, end: date) -> Figure:
    """Interest accrued over a window — the deductible part of the payment.

    Principal repayment is not an expense; only the interest is. Reporting the
    whole payment as a cost is one of the commonest ways a rental P&L ends up
    wrong.
    """
    if start >= end:
        return unknown("mortgage interest", note="empty date range")
    opening = principal_at(mortgage, start)
    rate = _periodic_rate(mortgage)
    payment = mortgage.payment_amount
    if not opening.known or rate is None or payment is None:
        return unknown(
            "mortgage interest", note="needs a balance, a rate and a payment"
        )

    balance = Decimal(opening.value)
    payment = Decimal(payment)
    total = Decimal("0")
    periods = int(
        (end - start).days / (Decimal("365.25") / Decimal(payments_per_year(mortgage)))
    )
    for _ in range(max(periods, 0)):
        interest = balance * rate
        total += interest
        balance = balance + interest - payment
        if balance <= 0:
            break
    return Figure(
        value=_money(total),
        label="mortgage interest",
        provenance=Provenance(
            source_type=SourceType.ESTIMATE,
            as_of=end,
            ref=str(mortgage.pk),
            note=f"{start.isoformat()} to {end.isoformat()}, from the contracted payment",
        ),
    )


def latest_valuation(holding):
    """The most recent valuation row, or None."""
    return holding.valuations.order_by("-as_of").first()


def active_mortgage(holding):
    from rentium.ledger.models import HoldingMortgage

    return holding.mortgages.filter(status=HoldingMortgage.Status.ACTIVE).first()


def equity_at(holding, on: date | None = None) -> Figure:
    """Value minus what is owed.

    Always an estimate, and the provenance says why: the valuation and the
    balance are measured on different days, and at least one of them is
    usually someone's opinion.
    """
    on = on or date.today()
    valuation = latest_valuation(holding)
    if valuation is None:
        return unknown("equity", note="no valuation on file for this property")

    mortgage = active_mortgage(holding)
    if mortgage is None:
        # No mortgage recorded is not the same as no mortgage. Say which.
        return Figure(
            value=_money(Decimal(valuation.amount)),
            label="equity",
            provenance=Provenance(
                source_type=SourceType.ESTIMATE,
                as_of=on,
                note=(
                    f"{valuation.get_basis_display().lower()} as of "
                    f"{valuation.as_of.isoformat()}; no mortgage on file"
                ),
            ),
        )

    balance = principal_at(mortgage, on)
    if not balance.known:
        return unknown("equity", note="no mortgage balance on file")
    return Figure(
        value=_money(Decimal(valuation.amount) - Decimal(balance.value)),
        label="equity",
        provenance=Provenance(
            source_type=SourceType.ESTIMATE,
            as_of=on,
            note=(
                f"{valuation.get_basis_display().lower()} as of "
                f"{valuation.as_of.isoformat()}, less a balance "
                f"{balance.provenance.note or 'on file'}"
            ),
        ),
    )


def renewal_horizon(mortgage, today: date | None = None) -> Figure:
    """Days until the term ends — when the rate is repriced."""
    today = today or date.today()
    if mortgage is None or mortgage.term_end is None:
        return unknown("renewal date", unit="days", note="no term end on file")
    return Figure(
        value=Decimal((mortgage.term_end - today).days),
        unit="days",
        label="until renewal",
        provenance=Provenance(
            source_type=SourceType.LANDLORD,
            as_of=mortgage.term_end,
            ref=str(mortgage.pk),
        ),
    )
