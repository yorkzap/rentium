"""
The financial data layer: what a property cost, is worth, and owes.

Two properties are load-bearing throughout:

- An unknown figure is None, never zero. Treating a missing mortgage balance as
  $0 would report the landlord as owning the house outright.
- Every number carries where it came from, because "you have $180k of equity"
  means something different if the valuation is a 2019 appraisal.
"""

from __future__ import annotations

import datetime
import json
from decimal import Decimal

import pytest

from rentium.ledger.models import HoldingMortgage, HoldingValuation
from rentium.rama import mortgage as mortgage_maths
from rentium.rama import tax
from rentium.rama.models import LandlordFinancialProfile, TaxRateTable
from rentium.rama.render import Figure, Provenance, SourceType

pytestmark = pytest.mark.django_db

TODAY = datetime.date(2026, 7, 1)


def _holding(landlord, name="950 McKenzie Ave"):
    from rentium.properties.models import PropertyHolding

    return PropertyHolding.objects.create(
        landlord=landlord, name=name, address=name, city="Victoria"
    )


def _mortgage(landlord, holding, **kwargs):
    defaults = dict(
        landlord=landlord,
        holding=holding,
        lender="Test Credit Union",
        current_principal=Decimal("400000.00"),
        current_principal_as_of=datetime.date(2026, 1, 1),
        rate_percent=Decimal("4.500"),
        rate_type=HoldingMortgage.RateType.FIXED,
        payment_amount=Decimal("2200.00"),
        payment_frequency="MONTHLY",
        term_end=datetime.date(2028, 3, 1),
    )
    defaults.update(kwargs)
    return HoldingMortgage.objects.create(**defaults)


def _valuation(landlord, holding, amount="700000.00", as_of=datetime.date(2026, 1, 1)):
    return HoldingValuation.objects.create(
        landlord=landlord,
        holding=holding,
        as_of=as_of,
        amount=Decimal(amount),
        basis=HoldingValuation.Basis.BC_ASSESSMENT,
    )


# ------------------------------------------------------------- Figure basics
def test_an_unknown_figure_is_none_not_zero():
    from rentium.rama.render import unknown

    figure = unknown("mortgage balance", note="nothing on file")
    assert figure.value is None
    assert figure.known is False
    assert "unknown" in figure.render()


def test_a_figure_renders_with_its_provenance():
    figure = Figure(
        value=Decimal("18000"),
        provenance=Provenance(
            source_type=SourceType.WEB, as_of=datetime.date(2026, 7, 1)
        ),
    )
    rendered = figure.render()
    assert "$18,000.00" in rendered
    assert "researched" in rendered
    assert "2026-07-01" in rendered


# ------------------------------------------------------------------ mortgage
def test_a_balance_before_the_known_date_is_reported_as_known(landlord):
    holding = _holding(landlord)
    m = _mortgage(landlord, holding)

    figure = mortgage_maths.principal_at(m, datetime.date(2026, 1, 1))
    assert figure.value == Decimal("400000.00")
    assert figure.provenance.source_type == SourceType.LANDLORD


def test_a_projected_balance_says_it_is_projected(landlord):
    holding = _holding(landlord)
    m = _mortgage(landlord, holding)

    figure = mortgage_maths.principal_at(m, TODAY)
    assert figure.value < Decimal("400000.00")  # six payments in
    assert figure.provenance.source_type == SourceType.ESTIMATE
    assert "projected from 2026-01-01" in figure.provenance.note


def test_a_balance_with_no_rate_is_not_projected_silently(landlord):
    """Reporting a six-month-old balance as current would be a lie of omission."""
    holding = _holding(landlord)
    m = _mortgage(landlord, holding, rate_percent=None)

    figure = mortgage_maths.principal_at(m, TODAY)
    assert figure.value == Decimal("400000.00")
    assert "not projected" in figure.provenance.note
    assert figure.provenance.as_of == datetime.date(2026, 1, 1)


def test_a_missing_balance_is_unknown(landlord):
    holding = _holding(landlord)
    m = _mortgage(landlord, holding, current_principal=None)

    assert mortgage_maths.principal_at(m, TODAY).known is False


def test_canadian_semi_annual_compounding_is_used(landlord):
    """US monthly compounding would overstate interest on every mortgage here."""
    holding = _holding(landlord)
    m = _mortgage(landlord, holding)

    rate = mortgage_maths._periodic_rate(m)
    us_monthly = Decimal("4.5") / Decimal("100") / Decimal("12")
    assert rate < us_monthly


def test_interest_is_separated_from_principal(landlord):
    """Only interest is deductible; the whole payment is not a cost."""
    holding = _holding(landlord)
    m = _mortgage(landlord, holding)

    interest = mortgage_maths.interest_paid_between(
        m, datetime.date(2026, 1, 1), datetime.date(2027, 1, 1)
    )
    total_paid = Decimal("2200.00") * 12
    assert interest.known
    assert Decimal("0") < interest.value < total_paid


def test_renewal_horizon_counts_days(landlord):
    holding = _holding(landlord)
    m = _mortgage(landlord, holding)

    figure = mortgage_maths.renewal_horizon(m, TODAY)
    assert figure.value == Decimal((datetime.date(2028, 3, 1) - TODAY).days)


# -------------------------------------------------------------------- equity
def test_equity_is_valuation_minus_balance_and_says_both_dates(landlord):
    holding = _holding(landlord)
    _mortgage(landlord, holding)
    _valuation(landlord, holding, "700000.00")

    figure = mortgage_maths.equity_at(holding, TODAY)
    assert figure.known
    assert figure.value > Decimal("300000")
    assert figure.provenance.source_type == SourceType.ESTIMATE
    assert "bc assessment" in figure.provenance.note.lower()
    assert "projected from" in figure.provenance.note


def test_equity_without_a_valuation_is_unknown(landlord):
    holding = _holding(landlord)
    _mortgage(landlord, holding)

    figure = mortgage_maths.equity_at(holding, TODAY)
    assert figure.known is False
    assert "no valuation" in figure.render()


def test_no_mortgage_on_file_is_stated_not_assumed(landlord):
    """'No mortgage recorded' and 'no mortgage' are different claims."""
    holding = _holding(landlord)
    _valuation(landlord, holding, "700000.00")

    figure = mortgage_maths.equity_at(holding, TODAY)
    assert figure.value == Decimal("700000.00")
    assert "no mortgage on file" in figure.provenance.note


def test_valuations_are_a_history_not_a_single_number(landlord):
    """PropertyBankBalance is overwrite-only, which is why no trend exists for
    it. Valuations must not repeat that."""
    holding = _holding(landlord)
    _valuation(landlord, holding, "650000.00", datetime.date(2024, 1, 1))
    _valuation(landlord, holding, "700000.00", datetime.date(2026, 1, 1))

    assert holding.valuations.count() == 2
    assert mortgage_maths.latest_valuation(holding).amount == Decimal("700000.00")


def test_only_one_mortgage_can_be_active(landlord):
    from django.db import IntegrityError, transaction

    holding = _holding(landlord)
    _mortgage(landlord, holding)
    with pytest.raises(IntegrityError):
        with transaction.atomic():
            _mortgage(landlord, holding)


# ----------------------------------------------------------------------- tax
def _brackets(landlord, year=2026, province="BC"):
    payload = [
        {"upto": 55867, "rate": 0.15},
        {"upto": 111733, "rate": 0.205},
        {"upto": None, "rate": 0.26},
    ]
    TaxRateTable.objects.create(
        jurisdiction="CA-FED", tax_year=year, kind="PERSONAL_INCOME_BRACKETS",
        payload=payload,
    )
    TaxRateTable.objects.create(
        jurisdiction=f"CA-{province}", tax_year=year, kind="PERSONAL_INCOME_BRACKETS",
        payload=[{"upto": 47937, "rate": 0.0506}, {"upto": None, "rate": 0.077}],
    )


def test_no_consent_means_no_tax_figure(landlord):
    LandlordFinancialProfile.objects.create(
        landlord=landlord, employment_income_band="B50_100K"
    )
    _brackets(landlord)

    assert tax.marginal_rate_estimate(landlord, year=2026) is None
    assert "consent" in tax.rate_unavailable_reason(landlord, year=2026)


def test_a_self_reported_rate_is_preferred(landlord):
    """Less revealing than a salary and more accurate than deriving one."""
    from django.utils import timezone

    LandlordFinancialProfile.objects.create(
        landlord=landlord,
        consented_at=timezone.now(),
        self_reported_marginal_rate=Decimal("38.00"),
    )

    figure = tax.marginal_rate_estimate(landlord, year=2026)
    assert figure.value == Decimal("38.00")
    assert figure.provenance.source_type == SourceType.LANDLORD


def test_a_band_plus_tables_gives_an_estimate(landlord):
    from django.utils import timezone

    landlord.province = "BC"
    landlord.save(update_fields=["province"])
    LandlordFinancialProfile.objects.create(
        landlord=landlord,
        consented_at=timezone.now(),
        employment_income_band="B50_100K",
        tax_province="BC",
    )
    _brackets(landlord, year=2026)

    figure = tax.marginal_rate_estimate(landlord, year=2026)
    assert figure is not None
    assert figure.provenance.source_type == SourceType.TAX_TABLE
    assert "midpoint" in figure.provenance.note


def test_a_missing_year_never_falls_back_to_another(landlord):
    """The whole point: quoting last year's brackets still looks right."""
    from django.utils import timezone

    LandlordFinancialProfile.objects.create(
        landlord=landlord,
        consented_at=timezone.now(),
        employment_income_band="B50_100K",
        tax_province="BC",
    )
    _brackets(landlord, year=2026)

    assert tax.marginal_rate_estimate(landlord, year=2027) is None
    reason = tax.rate_unavailable_reason(landlord, year=2027)
    assert "2027" in reason
    assert "won't apply another year" in reason


def test_the_disclaimer_is_not_the_models_to_write():
    assert "not tax advice" in tax.DISCLAIMER


# --------------------------------------------------------- the loader command
def test_the_loader_rejects_percentages_given_as_whole_numbers(tmp_path):
    from django.core.management import call_command
    from django.core.management.base import CommandError

    path = tmp_path / "b.json"
    path.write_text(json.dumps([{"upto": None, "rate": 15}]))  # 15 not 0.15
    with pytest.raises(CommandError, match="fraction"):
        call_command(
            "rama_load_tax_table", "--jurisdiction", "CA-BC", "--year", "2027",
            "--kind", "PERSONAL_INCOME_BRACKETS", "--file", str(path),
        )


def test_the_loader_requires_an_open_final_bracket(tmp_path):
    from django.core.management import call_command
    from django.core.management.base import CommandError

    path = tmp_path / "b.json"
    path.write_text(json.dumps([{"upto": 50000, "rate": 0.15}]))
    with pytest.raises(CommandError, match="final bracket"):
        call_command(
            "rama_load_tax_table", "--jurisdiction", "CA-BC", "--year", "2027",
            "--kind", "PERSONAL_INCOME_BRACKETS", "--file", str(path),
        )


def test_the_loader_will_not_silently_overwrite(tmp_path):
    from django.core.management import call_command
    from django.core.management.base import CommandError

    path = tmp_path / "b.json"
    path.write_text(json.dumps([{"upto": None, "rate": 0.15}]))
    args = [
        "--jurisdiction", "CA-BC", "--year", "2027",
        "--kind", "PERSONAL_INCOME_BRACKETS", "--file", str(path),
    ]
    call_command("rama_load_tax_table", *args)
    with pytest.raises(CommandError, match="already loaded"):
        call_command("rama_load_tax_table", *args)
    call_command("rama_load_tax_table", *args, "--replace")
    assert TaxRateTable.objects.filter(jurisdiction="CA-BC", tax_year=2027).count() == 1


# ------------------------------------------------ who may write these numbers
def test_the_treasurer_cannot_write_any_of_this(landlord):
    """The separation that matters: the agent concluding "your equity looks
    strong" must not also be able to adjust the valuation it rests on."""
    from rentium.rama.roles import role_allows_tool

    for tool in ("record_holding_financials", "record_valuation", "record_mortgage"):
        assert role_allows_tool("treasurer", tool) is False
        assert role_allows_tool("general", tool) is True


def test_the_treasurer_can_read_them(landlord):
    holding = _holding(landlord)
    _valuation(landlord, holding, "700000.00")
    figure = mortgage_maths.equity_at(holding, TODAY)
    assert figure.known


def test_recording_a_valuation_adds_rather_than_replaces(landlord):
    from rentium.rama import registry

    holding = _holding(landlord)
    _valuation(landlord, holding, "650000.00", datetime.date(2024, 1, 1))

    registry.execute(
        "record_valuation",
        {
            "holding_name": holding.name,
            "amount": "700000",
            "as_of": "2026-01-01",
            "basis": "BC_ASSESSMENT",
            "confirm": "yes",
        },
        landlord=landlord,
    )
    assert holding.valuations.count() == 2


def test_recording_a_mortgage_supersedes_rather_than_edits(landlord):
    from rentium.rama import registry

    holding = _holding(landlord)
    old = _mortgage(landlord, holding, rate_percent=Decimal("2.100"))

    registry.execute(
        "record_mortgage",
        {
            "holding_name": holding.name,
            "current_principal": "390000",
            "balance_as_of": "2026-06-01",
            "rate_percent": "5.2",
            "payment_amount": "2400",
            "term_end": "2031-06-01",
            "confirm": "yes",
        },
        landlord=landlord,
    )
    old.refresh_from_db()
    assert old.status == HoldingMortgage.Status.SUPERSEDED
    assert old.rate_percent == Decimal("2.100")  # history intact
    assert holding.mortgages.filter(status=HoldingMortgage.Status.ACTIVE).count() == 1


def test_a_balance_without_its_date_is_refused(landlord):
    """A balance with no as-of date cannot be aged, so it would be reported as
    current forever."""
    from rentium.rama import registry

    holding = _holding(landlord)
    out = registry.execute(
        "record_mortgage",
        {"holding_name": holding.name, "current_principal": "400000", "confirm": "yes"},
        landlord=landlord,
    )
    assert "balance_as_of" in out["error"]


def test_a_rate_given_in_basis_points_is_refused(landlord):
    from rentium.rama import registry

    holding = _holding(landlord)
    out = registry.execute(
        "record_mortgage",
        {
            "holding_name": holding.name,
            "rate_percent": "450",
            "confirm": "yes",
        },
        landlord=landlord,
    )
    assert "basis points" in out["error"]


def test_these_writes_preview_before_applying(landlord):
    from rentium.rama import registry

    holding = _holding(landlord)
    out = registry.execute(
        "record_valuation",
        {"holding_name": holding.name, "amount": "700000"},
        landlord=landlord,
    )
    assert out["needs_confirm"] is True
    assert holding.valuations.count() == 0
