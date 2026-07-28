"""
Financial facts the ledger does not have.

The motivating case, from a real conversation: "you're missing that we took
$2,000 rent from another tenant for a year, which you haven't factored in."

Recording that naively is dangerous. If any of that rent WAS in the ledger,
adding the assertion on top double-counts it — and a double-counted income
figure is worse than a missing one, because it looks like good news. So the
load-bearing tests here are the reconciliation ones.
"""

from __future__ import annotations

import datetime
from decimal import Decimal

import pytest

from rentium.ledger.models import EntryType, ExpenseCategory, LedgerEntry
from rentium.rama import registry
from rentium.rama import treasurer_facts as facts
from rentium.rama.models import TreasurerFact
from rentium.rama.render import Figure, Provenance, SourceType, substitute

pytestmark = pytest.mark.django_db

YEAR_START = datetime.date(2024, 4, 1)
YEAR_END = datetime.date(2025, 3, 31)


def _holding(landlord, name="950 McKenzie Ave"):
    from rentium.properties.models import PropertyHolding

    return PropertyHolding.objects.create(
        landlord=landlord, name=name, address=name, city="Victoria"
    )


def _tenant():
    """One tenant, reused — charges must name who owes them."""
    from rentium.users.models import TenantProfile
    from rentium.users.tests.factories import UserFactory

    existing = TenantProfile.objects.first()
    return existing or TenantProfile.objects.create(user=UserFactory())


def _payment(landlord, holding, amount, on):
    """A payment that actually landed, scoped to a holding.

    The ledger requires a settlement to name the charge it settles, and a
    charge to name who owes it, so this posts both — which is also what the
    real world looks like.
    """
    charge = LedgerEntry.objects.create(
        landlord=landlord,
        holding=holding,
        tenant=_tenant(),
        entry_type=EntryType.OTHER_CHARGE,
        amount=Decimal(amount),
        due_date=on,
        effective_date=on,
        description="Rent billed",
    )
    return LedgerEntry.objects.create(
        landlord=landlord,
        holding=holding,
        entry_type=EntryType.PAYMENT,
        amount=Decimal(amount),
        effective_date=on,
        settles=charge,
        description="Rent received",
    )


def _assert_fact(landlord, holding=None, **kwargs):
    defaults = dict(
        key="upstairs-rent-2024",
        subject="upstairs rent 2024",
        statement="We took $2,000/mo rent from another tenant upstairs for a year.",
        direction=TreasurerFact.Direction.INCOME,
        value_numeric=Decimal("2000.00"),
        period=TreasurerFact.Period.MONTHLY,
        effective_from=YEAR_START,
        effective_to=YEAR_END,
        holding=holding,
    )
    defaults.update(kwargs)
    return facts.write(landlord, **defaults)


# --------------------------------------------------------------- the arithmetic
def test_a_monthly_assertion_totals_over_its_period(landlord):
    holding = _holding(landlord)
    fact = _assert_fact(landlord, holding)
    total = facts.asserted_total(fact)
    assert Decimal("23000") < total < Decimal("24500")  # ~$2,000 x 12


def test_a_one_time_assertion_is_just_the_amount(landlord):
    holding = _holding(landlord)
    fact = _assert_fact(
        landlord,
        holding,
        key="roof-2024",
        period=TreasurerFact.Period.ONE_TIME,
        value_numeric=Decimal("8000.00"),
        direction=TreasurerFact.Direction.EXPENSE,
    )
    assert facts.asserted_total(fact) == Decimal("8000.00")


# ------------------------------------------------------------- reconciliation
def test_a_genuine_gap_is_counted(landlord):
    """Nothing in the books for that period — the assertion is usable."""
    holding = _holding(landlord)
    fact = _assert_fact(landlord, holding)

    assert fact.double_count_risk is False
    assert fact.ledger_overlap_amount == Decimal("0.00")

    pack = facts.render_for_pack(landlord)
    assert len(pack["usable"]) == 1
    assert pack["shown_not_counted"] == []


def test_money_already_in_the_books_is_flagged_and_excluded(landlord):
    """The whole point. If the ledger already holds it, counting the
    assertion too would overstate income."""
    holding = _holding(landlord)
    for month in range(12):
        _payment(
            landlord, holding, "2000.00",
            YEAR_START + datetime.timedelta(days=30 * month),
        )

    fact = _assert_fact(landlord, holding)

    assert fact.double_count_risk is True
    assert fact.ledger_overlap_amount >= Decimal("22000.00")

    pack = facts.render_for_pack(landlord)
    assert pack["usable"] == []
    assert len(pack["shown_not_counted"]) == 1
    assert "already record" in pack["shown_not_counted"][0]["why_not_counted"]
    assert "must NOT be added to any total" in pack["instruction"]


def test_a_partial_overlap_is_still_counted(landlord):
    """Two months recorded out of twelve is a real gap, not a double-count."""
    holding = _holding(landlord)
    for month in range(2):
        _payment(
            landlord, holding, "2000.00",
            YEAR_START + datetime.timedelta(days=30 * month),
        )

    fact = _assert_fact(landlord, holding)
    assert fact.double_count_risk is False
    assert fact.ledger_overlap_amount == Decimal("4000.00")


def test_payments_outside_the_window_do_not_count_as_overlap(landlord):
    holding = _holding(landlord)
    for month in range(12):
        _payment(
            landlord, holding, "2000.00",
            datetime.date(2020, 1, 1) + datetime.timedelta(days=30 * month),
        )

    fact = _assert_fact(landlord, holding)
    assert fact.double_count_risk is False


def test_income_is_not_reconciled_against_expenses(landlord):
    """Comparing unlike things would produce a nonsense overlap."""
    holding = _holding(landlord)
    LedgerEntry.objects.create(
        landlord=landlord, holding=holding, entry_type=EntryType.EXPENSE,
        amount=Decimal("24000.00"), effective_date=YEAR_START,
        category=ExpenseCategory.MAINTENANCE, description="Big repair",
    )

    fact = _assert_fact(landlord, holding)
    assert fact.double_count_risk is False


def test_another_holding_is_not_overlap(landlord):
    here = _holding(landlord)
    elsewhere = _holding(landlord, "3213 Wascana St")
    for month in range(12):
        _payment(
            landlord, elsewhere, "2000.00",
            YEAR_START + datetime.timedelta(days=30 * month),
        )

    fact = _assert_fact(landlord, here)
    assert fact.double_count_risk is False


def test_a_portfolio_wide_fact_reconciles_across_everything(landlord):
    """No scope is a legitimate scope — it means all of this landlord."""
    holding = _holding(landlord)
    for month in range(12):
        _payment(
            landlord, holding, "2000.00",
            YEAR_START + datetime.timedelta(days=30 * month),
        )

    fact = _assert_fact(landlord, holding=None)
    assert fact.double_count_risk is True


def test_a_neutral_fact_is_not_reconciled(landlord):
    """A rate or a count has nothing to compare against."""
    fact = _assert_fact(
        landlord,
        key="cap-rate",
        statement="We target a 5% cap rate.",
        direction=TreasurerFact.Direction.NEUTRAL,
        value_numeric=Decimal("5.00"),
        period=TreasurerFact.Period.ONE_TIME,
        effective_from=None,
        effective_to=None,
    )
    assert fact.ledger_overlap_amount is None
    assert fact.double_count_risk is False


def test_facts_are_landlord_scoped(landlord, other_landlord):
    holding = _holding(landlord)
    _assert_fact(landlord, holding)
    assert facts.render_for_pack(other_landlord)["usable"] == []


# --------------------------------------------------------------- supersession
def test_a_correction_supersedes_rather_than_duplicating(landlord):
    holding = _holding(landlord)
    first = _assert_fact(landlord, holding)
    second = _assert_fact(
        landlord, holding, statement="Actually it was $1,800/mo.",
        value_numeric=Decimal("1800.00"),
    )

    active = TreasurerFact.objects.filter(
        landlord=landlord, status=TreasurerFact.Status.ACTIVE
    )
    assert active.count() == 1
    assert active.first().pk == second.pk
    first.refresh_from_db()
    assert first.status == TreasurerFact.Status.SUPERSEDED
    assert second.supersedes_id == first.pk


def test_two_active_facts_on_one_key_are_impossible(landlord):
    from django.db import IntegrityError, transaction

    _assert_fact(landlord, _holding(landlord))
    with pytest.raises(IntegrityError):
        with transaction.atomic():
            TreasurerFact.objects.create(
                landlord=landlord, key="upstairs-rent-2024",
                subject="dupe", statement="contradiction",
                kind=TreasurerFact.Kind.LANDLORD_ASSERTED,
                confidence=TreasurerFact.Confidence.STATED,
            )


def test_retracting_removes_it_from_the_pack(landlord):
    holding = _holding(landlord)
    _assert_fact(landlord, holding)
    facts.retract(landlord, "upstairs-rent-2024")
    assert facts.render_for_pack(landlord)["usable"] == []


# --------------------------------------------------------------------- guards
def test_special_category_data_is_refused():
    assert facts.rejects("the tenant upstairs is on disability so pays less")


def test_a_portfolio_wide_amount_is_allowed():
    """Reconciliation works portfolio-wide, so requiring a property would
    refuse legitimate facts like 'we spent $5,000 on accounting'."""
    assert facts.rejects("We spent $5,000 on accounting fees", value_numeric=Decimal("5000")) is None


def test_an_overlong_statement_is_refused():
    assert facts.rejects("x" * 500)


@pytest.mark.parametrize(
    "text,expected_period",
    [
        ("$2,000/mo", TreasurerFact.Period.MONTHLY),
        ("2000 per month", TreasurerFact.Period.MONTHLY),
        ("$5,000 a year", TreasurerFact.Period.ANNUAL),
        ("$8,000", TreasurerFact.Period.ONE_TIME),
    ],
)
def test_amounts_and_periods_are_parsed(text, expected_period):
    amount, period = facts.parse_amount(text)
    assert amount is not None
    assert period == expected_period


def test_direction_is_inferred_from_the_words():
    assert facts.infer_direction("we took rent from a tenant") == TreasurerFact.Direction.INCOME
    assert facts.infer_direction("we spent it on a new roof") == TreasurerFact.Direction.EXPENSE
    assert facts.infer_direction("the cap rate is 5%") == TreasurerFact.Direction.NEUTRAL


# ----------------------------------------------------------------- the tool
def test_the_tool_warns_about_a_double_count_before_confirming(landlord):
    """The warning must land in the PREVIEW — after the fact is on file is too
    late to be useful."""
    holding = _holding(landlord)
    for month in range(12):
        _payment(
            landlord, holding, "2000.00",
            YEAR_START + datetime.timedelta(days=30 * month),
        )

    preview = registry.execute(
        "record_treasurer_fact",
        {
            "subject": "upstairs rent 2024",
            "fact": "We took $2,000/mo from another tenant upstairs for a year.",
            "amount": "2000",
            "period": "MONTHLY",
            "direction": "INCOME",
            "holding_name": holding.name,
            "effective_from": YEAR_START.isoformat(),
            "effective_to": YEAR_END.isoformat(),
        },
        landlord=landlord,
    )
    assert preview["needs_confirm"] is True
    assert "double_count_warning" in preview["preview"]
    assert not TreasurerFact.objects.filter(landlord=landlord).exists()


def test_the_tool_records_a_genuine_gap(landlord):
    holding = _holding(landlord)
    out = registry.execute(
        "record_treasurer_fact",
        {
            "subject": "upstairs rent 2024",
            "fact": "We took $2,000/mo from another tenant upstairs for a year.",
            "amount": "2000",
            "period": "MONTHLY",
            "direction": "INCOME",
            "holding_name": holding.name,
            "effective_from": YEAR_START.isoformat(),
            "effective_to": YEAR_END.isoformat(),
            "confirm": "yes",
        },
        landlord=landlord,
    )
    assert out["recorded"] is True
    assert out["counted_in_totals"] is True


def test_a_periodic_amount_without_dates_is_refused(landlord):
    holding = _holding(landlord)
    out = registry.execute(
        "record_treasurer_fact",
        {
            "subject": "rent", "fact": "we took $2,000/mo", "amount": "2000",
            "period": "MONTHLY", "holding_name": holding.name, "confirm": "yes",
        },
        landlord=landlord,
    )
    assert "effective_from" in out["error"]


def test_the_treasurer_cannot_record_facts_itself(landlord):
    """It reasons over facts; the landlord asserts them through the General."""
    from rentium.rama.roles import role_allows_tool

    assert role_allows_tool("treasurer", "record_treasurer_fact") is False
    assert role_allows_tool("general", "record_treasurer_fact") is True


# ------------------------------------------------------- token substitution
def test_prose_cannot_contain_a_number_the_model_typed():
    table = {
        "f1": Figure(
            value=Decimal("18000"),
            provenance=Provenance(source_type=SourceType.WEB,
                                  as_of=datetime.date(2026, 7, 1)),
        )
    }
    text, violations = substitute("The install runs about {{f1}} before rebate.", table)
    assert "$18,000.00" in text
    assert "researched" in text  # the caveat cannot be dropped
    assert violations == []


def test_an_invented_token_is_a_violation():
    text, violations = substitute("It costs {{f9}}.", {})
    assert violations
    assert "[f9?]" in text


@pytest.fixture
def other_landlord():
    from rentium.users.models import LandlordProfile
    from rentium.users.tests.factories import UserFactory

    return LandlordProfile.objects.create(user=UserFactory())


# ============================================ the chain of command (Step 7)
def test_a_request_reaches_the_general_verbatim(landlord):
    """The Treasurer has no channel of its own. What it needs must arrive via
    the Chief, worded as the Treasurer wrote it."""
    from rentium.rama.models import TreasurerRequest
    from rentium.rama.roles import role_context

    TreasurerRequest.objects.create(
        landlord=landlord,
        question="What has an installer quoted for the heat pump?",
        why_it_matters="It decides whether windows or the heat pump comes first.",
    )
    context = role_context("general", landlord)
    assert "TREASURER REQUESTS" in context
    assert "What has an installer quoted for the heat pump?" in context
    assert "relay these VERBATIM" in context


def test_the_general_is_told_never_to_answer_one_itself(landlord):
    from rentium.rama.roles import GENERAL_PROMPT

    assert "TREASURER REQUESTS" in GENERAL_PROMPT
    assert "never guess the figure" in GENERAL_PROMPT


def test_relaying_marks_the_request_relayed(landlord):
    from rentium.rama.models import TreasurerRequest
    from rentium.rama.roles import role_context

    request = TreasurerRequest.objects.create(
        landlord=landlord, question="What did the roof cost?"
    )
    role_context("general", landlord)
    request.refresh_from_db()
    assert request.status == TreasurerRequest.Status.RELAYED
    assert request.relayed_at is not None


def test_no_requests_means_no_block(landlord):
    """An empty section would be noise in every single General turn."""
    from rentium.rama.roles import role_context

    assert "TREASURER REQUESTS" not in role_context("general", landlord)


def test_the_treasurer_does_not_see_the_request_block(landlord):
    """It wrote them; relaying is the Chief's job."""
    from rentium.rama.models import TreasurerRequest
    from rentium.rama.roles import role_context

    TreasurerRequest.objects.create(landlord=landlord, question="anything?")
    assert role_context("treasurer", landlord) == ""


def test_an_expired_request_stops_being_relayed(landlord):
    from datetime import timedelta

    from django.utils import timezone

    from rentium.rama.models import TreasurerRequest
    from rentium.rama.roles import role_context

    TreasurerRequest.objects.create(
        landlord=landlord,
        question="stale question",
        expires_at=timezone.now() - timedelta(days=1),
    )
    assert "stale question" not in role_context("general", landlord)


def test_requests_appear_in_the_morning_briefing(landlord):
    from rentium.rama.deliberation import briefing_section
    from rentium.rama.models import TreasurerRequest

    TreasurerRequest.objects.create(landlord=landlord, question="What did the roof cost?")
    lines = briefing_section(landlord)
    assert lines[0].startswith("Treasurer needs")
    assert any("roof" in line for line in lines)


def test_requests_are_landlord_scoped(landlord, other_landlord):
    from rentium.rama.models import TreasurerRequest
    from rentium.rama.roles import role_context

    TreasurerRequest.objects.create(landlord=other_landlord, question="not yours")
    assert "not yours" not in role_context("general", landlord)


# ------------------------------------------------------ the finance watchers
def test_a_looming_renewal_is_noticed(landlord):
    """The one dated financial decision a landlord can miss."""
    import datetime

    from rentium.events.models import DomainEvent
    from rentium.ledger.models import HoldingMortgage
    from rentium.rama import sergeants

    holding = _holding(landlord)
    HoldingMortgage.objects.create(
        landlord=landlord, holding=holding,
        term_end=datetime.date.today() + datetime.timedelta(days=60),
        rate_percent="4.500",
    )
    assert sergeants.check_mortgage_renewals()["findings_published"] == 1
    assert DomainEvent.objects.filter(
        event_type="rama.sentinel.mortgage_renewal"
    ).exists()
    # Idempotent — a daily beat must not notify every morning.
    assert sergeants.check_mortgage_renewals()["findings_published"] == 0


def test_a_renewal_far_away_is_not_flagged(landlord):
    import datetime

    from rentium.ledger.models import HoldingMortgage
    from rentium.rama import sergeants

    HoldingMortgage.objects.create(
        landlord=landlord, holding=_holding(landlord),
        term_end=datetime.date.today() + datetime.timedelta(days=900),
    )
    assert sergeants.check_mortgage_renewals()["findings_published"] == 0


def test_a_years_old_valuation_is_flagged(landlord):
    import datetime
    from decimal import Decimal as D

    from rentium.ledger.models import HoldingValuation
    from rentium.rama import sergeants

    holding = _holding(landlord)
    HoldingValuation.objects.create(
        landlord=landlord, holding=holding,
        as_of=datetime.date.today() - datetime.timedelta(days=1200),
        amount=D("650000"), basis=HoldingValuation.Basis.BC_ASSESSMENT,
    )
    assert sergeants.check_valuation_staleness()["findings_published"] == 1


def test_a_recent_valuation_is_not_flagged(landlord):
    import datetime
    from decimal import Decimal as D

    from rentium.ledger.models import HoldingValuation
    from rentium.rama import sergeants

    HoldingValuation.objects.create(
        landlord=landlord, holding=_holding(landlord),
        as_of=datetime.date.today(), amount=D("700000"),
        basis=HoldingValuation.Basis.BC_ASSESSMENT,
    )
    assert sergeants.check_valuation_staleness()["findings_published"] == 0


def test_every_new_sentinel_type_is_dispatched():
    """The event registry has no prefix matching — a type missing from this
    tuple publishes and is silently never analysed."""
    from rentium.rama.handlers import SENTINEL_EVENT_TYPES
    from rentium.rama.tasks import _KIND_BRIEF

    for event_type in (
        "rama.sentinel.mortgage_renewal",
        "rama.sentinel.valuation_stale",
        "rama.sentinel.spend_drift",
    ):
        assert event_type in SENTINEL_EVENT_TYPES
        assert event_type in _KIND_BRIEF


def test_the_morning_briefing_carries_the_requests(landlord):
    """The one place a landlord reliably reads every day — and $0, since
    briefing_section is pure Python."""
    from rentium.rama.briefing import build_briefing_text
    from rentium.rama.models import TreasurerRequest

    TreasurerRequest.objects.create(
        landlord=landlord, question="What did the roof cost in 2019?"
    )
    text = build_briefing_text(landlord)
    assert "Treasurer needs from you:" in text
    assert "roof" in text


def test_a_quiet_treasurer_adds_nothing_to_the_briefing(landlord):
    from rentium.rama.briefing import build_briefing_text

    assert "Treasurer needs" not in build_briefing_text(landlord)
