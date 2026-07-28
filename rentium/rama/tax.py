"""
Tax, as planning estimates only.

Two rules, both structural rather than advisory:

1. **Never fall back to a previous year.** If no rate table is loaded for the
   year in question, this returns nothing and the caller omits every tax
   figure. Saying "I can't estimate 2027 tax until the brackets are loaded" is
   recoverable. Quietly applying 2026 brackets to 2027 income is not — the
   arithmetic still looks right, so nobody catches it.

2. **Never read the landlord's profile without consent.** Absent consent the
   estimate is unavailable, and the reason given is the missing consent, not a
   vague failure.

Every figure produced here carries the ESTIMATE label and its assumptions, and
`DISCLAIMER` is rendered by Python so a model cannot drop it.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from .render import Figure, Provenance, SourceType

DISCLAIMER = (
    "ESTIMATE — planning only, not tax advice. Confirm with your accountant "
    "before filing or making a decision on it."
)


def _profile(landlord):
    from .models import LandlordFinancialProfile

    return LandlordFinancialProfile.objects.filter(landlord=landlord).first()


def _table(jurisdiction: str, tax_year: int, kind: str):
    from .models import TaxRateTable

    return TaxRateTable.objects.filter(
        jurisdiction=jurisdiction, tax_year=tax_year, kind=kind
    ).first()


# Midpoints used only when the landlord gave a band but not a rate. Stated as
# a band midpoint in the provenance so nobody mistakes it for their salary.
_BAND_MIDPOINTS = {
    "UNDER_50K": Decimal("35000"),
    "B50_100K": Decimal("75000"),
    "B100_150K": Decimal("125000"),
    "B150_250K": Decimal("200000"),
    "OVER_250K": Decimal("300000"),
}


def marginal_rate_estimate(landlord, *, year: int | None = None) -> Figure | None:
    """The landlord's combined marginal rate, or None if it cannot be estimated.

    None is a real answer with three distinct causes, and the caller should say
    which: no consent, nothing to work from, or no rate table for the year.
    """
    year = year or date.today().year
    profile = _profile(landlord)
    if profile is None or not profile.usable:
        return None

    # The preferred path: the landlord (or their accountant) told us the rate.
    # More accurate than deriving it, and less revealing than an exact salary.
    if profile.self_reported_marginal_rate is not None:
        return Figure(
            value=Decimal(profile.self_reported_marginal_rate),
            unit="percent",
            label="marginal tax rate",
            provenance=Provenance(
                source_type=SourceType.LANDLORD,
                note="the rate you gave us",
                as_of=date(year, 1, 1),
            ),
        )

    band = profile.employment_income_band
    if not band or band == "PREFER_NOT_TO_SAY":
        return None
    income = _BAND_MIDPOINTS.get(band)
    if income is None:
        return None

    province = (profile.tax_province or landlord.province or "").strip().upper()[:2]
    federal = _table("CA-FED", year, "PERSONAL_INCOME_BRACKETS")
    provincial = _table(f"CA-{province}", year, "PERSONAL_INCOME_BRACKETS") if province else None
    if federal is None or provincial is None:
        # Deliberately not "use last year's" — see the module docstring.
        return None

    rate = _top_rate(federal.payload, income) + _top_rate(provincial.payload, income)
    return Figure(
        value=(rate * Decimal("100")).quantize(Decimal("0.01")),
        unit="percent",
        label="marginal tax rate",
        provenance=Provenance(
            source_type=SourceType.TAX_TABLE,
            ref=f"CA-FED+{province} {year}",
            as_of=date(year, 1, 1),
            note=f"midpoint of your stated {band} band",
        ),
    )


def _top_rate(payload, income: Decimal) -> Decimal:
    """The rate that applies to the next dollar earned.

    Payload shape: [{"upto": 55867, "rate": 0.15}, ...] with the final bracket
    carrying "upto": null.
    """
    brackets = payload if isinstance(payload, list) else payload.get("brackets") or []
    rate = Decimal("0")
    for bracket in brackets:
        rate = Decimal(str(bracket.get("rate", 0)))
        upto = bracket.get("upto")
        if upto is None or income <= Decimal(str(upto)):
            return rate
    return rate


def rate_unavailable_reason(landlord, *, year: int | None = None) -> str:
    """Why no estimate is available — so the Treasurer can say it plainly."""
    year = year or date.today().year
    profile = _profile(landlord)
    if profile is None or not profile.usable:
        return (
            "I don't have your consent to use income details, so I'm leaving "
            "tax out. You can turn that on in Settings if you want tax-aware "
            "estimates."
        )
    if profile.self_reported_marginal_rate is None and (
        not profile.employment_income_band
        or profile.employment_income_band == "PREFER_NOT_TO_SAY"
    ):
        return (
            "I need either your marginal tax rate or an income band before I "
            "can put a tax figure on this."
        )
    province = (profile.tax_province or landlord.province or "").strip().upper()[:2]
    return (
        f"No {year} tax tables are loaded for CA-FED/CA-{province}, and I "
        f"won't apply another year's brackets to {year}. Once they're loaded "
        f"I can estimate this."
    )
