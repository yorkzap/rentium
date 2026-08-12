"""Aggregation on the generic `read` — the capability the failing question needed.

"How many rents did we receive for aug or are due?" was unanswerable. Not
because the data was missing, but because the only tool that could shape it,
`charge_schedule`, ranked 20th of 177 in a retrieval pass that shows the model
12 — so it never saw it. `read` WAS in front of it the whole time and could not
express the question: no aggregation, no grouping, and no way to say
paid-versus-due at all.

These tests pin the new grammar and, just as importantly, the safety invariants
it must not have loosened: scope, default-deny fields, no raw ORM, and totals
computed over the whole set rather than the page.
"""

from __future__ import annotations

from datetime import date
from datetime import timedelta

import pytest

from rentium.ledger import services as ledger_services
from rentium.ledger.models import EntryType
from rentium.ledger.models import PaymentMethod
from rentium.rama.domain_read import read

pytestmark = pytest.mark.django_db

AUG = date(2026, 8, 1)


@pytest.fixture
def other_landlord(db):
    from rentium.users.models import LandlordProfile
    from rentium.users.tests.factories import UserFactory

    return LandlordProfile.objects.create(user=UserFactory())


def _charge(landlord, lease, amount, due, entry_type=EntryType.RENT_CHARGE):
    charge, _ = ledger_services.post_charge(
        landlord=landlord,
        tenant=None,
        lease=lease,
        property=lease.property,
        amount=amount,
        due_date=due,
        entry_type=entry_type,
        description="rent",
    )
    return charge


@pytest.fixture
def august(landlord, bc_lease):
    """Three August rents: one paid, one part-paid, one still to come."""
    paid = _charge(landlord, bc_lease, "1000.00", AUG)
    partial = _charge(landlord, bc_lease, "900.00", AUG + timedelta(days=1))
    _future = _charge(landlord, bc_lease, "850.00", date(2026, 9, 1))
    _deposit = _charge(
        landlord, bc_lease, "425.00", AUG, EntryType.DEPOSIT_CHARGE,
    )
    ledger_services.record_payment(
        charge=paid, amount="1000.00",
        payment_method=PaymentMethod.ETRANSFER, payment_date=AUG,
    )
    ledger_services.record_payment(
        charge=partial, amount="300.00",
        payment_method=PaymentMethod.ETRANSFER, payment_date=AUG,
    )
    return {"paid": paid, "partial": partial}


# ------------------------------------------------------------- the real thing

def test_the_question_that_started_this(landlord, august):
    """One call, no retrieval, the whole answer."""
    result = read(
        landlord,
        entity="ledger_entry",
        filters="entry_type=RENT_CHARGE",
        month="2026-08",
        group_by="charge_state",
        aggregate="count, sum:amount, sum:outstanding",
    )
    assert "error" not in result
    by_state = {row["charge_state"]: row for row in result["groups"]}

    assert by_state["PAID"]["count"] == 1
    assert by_state["PAID"]["sum_amount"] == "1000.00"
    assert by_state["PAID"]["sum_outstanding"] == "0.00"

    assert by_state["PARTIALLY_PAID"]["count"] == 1
    assert by_state["PARTIALLY_PAID"]["sum_outstanding"] == "600.00"

    # September's rent is not August's question.
    assert "SCHEDULED" not in by_state
    # Nor is the deposit — it is a refundable liability, not rent.
    assert result["totals"]["count"] == 2
    assert result["filters_applied"] == "entry_type=RENT_CHARGE; due_date in 2026-08"


def test_arrears(landlord, august):
    result = read(
        landlord,
        entity="ledger_entry",
        filters="charge_state=OVERDUE",
        aggregate="count, sum:outstanding",
    )
    assert "error" not in result
    assert result["totals"]["count"] >= 1


# ------------------------------------------------------------------- grammar

def test_money_is_never_a_float(landlord, august):
    """A float total of somebody's rent is a rounding error waiting to be quoted."""
    totals = read(
        landlord, entity="ledger_entry", aggregate="sum:amount, avg:amount",
    )["totals"]
    assert all(isinstance(v, str) for v in (totals["sum_amount"], totals["avg_amount"]))


def test_totals_cover_everything_not_just_the_page(landlord, bc_lease):
    """The whole reason to aggregate in SQL rather than over fetched rows."""
    for index in range(12):
        _charge(landlord, bc_lease, "100.00", AUG + timedelta(days=index))

    result = read(
        landlord, entity="ledger_entry", limit="3", aggregate="count, sum:amount",
    )
    assert result["totals"]["count"] == 12
    assert result["totals"]["sum_amount"] == "1200.00"


def test_rows_report_their_own_truncation(landlord, bc_lease):
    for index in range(5):
        _charge(landlord, bc_lease, "100.00", AUG + timedelta(days=index))
    result = read(landlord, entity="ledger_entry", limit="2")
    assert result["returned"] == 2
    assert result["total_matched"] == 5
    assert result["truncated"] is True


def test_group_by_a_date_period(landlord, bc_lease):
    _charge(landlord, bc_lease, "100.00", date(2026, 7, 1))
    _charge(landlord, bc_lease, "200.00", date(2026, 8, 1))
    _charge(landlord, bc_lease, "300.00", date(2026, 8, 15))

    result = read(
        landlord, entity="ledger_entry",
        group_by="month:due_date", aggregate="count, sum:amount",
    )
    months = {str(row["month_due_date"])[:7]: row for row in result["groups"]}
    assert months["2026-07"]["sum_amount"] == "100.00"
    assert months["2026-08"]["sum_amount"] == "500.00"


def test_order_by_an_aggregate(landlord, august):
    result = read(
        landlord, entity="ledger_entry",
        group_by="entry_type", aggregate="count, sum:amount", order_by="-sum_amount",
    )
    amounts = [row["sum_amount"] for row in result["groups"]]
    assert amounts == sorted(amounts, key=float, reverse=True)


def test_relative_months_resolve_server_side(landlord, bc_lease):
    """Asked for "this month", a model reaches for a year it was trained on."""
    today = date.today()
    _charge(landlord, bc_lease, "777.00", today)
    result = read(
        landlord, entity="ledger_entry", month="this", aggregate="sum:amount",
    )
    assert result["totals"]["sum_amount"] == "777.00"
    assert f"{today:%Y-%m}" in result["filters_applied"]


def test_between_two_dates(landlord, bc_lease):
    _charge(landlord, bc_lease, "100.00", date(2026, 1, 15))
    _charge(landlord, bc_lease, "200.00", date(2026, 6, 15))
    result = read(
        landlord, entity="ledger_entry",
        between="2026-01-01..2026-03-31", aggregate="sum:amount",
    )
    assert result["totals"]["sum_amount"] == "100.00"


def test_group_by_with_no_aggregate_means_how_many(landlord, august):
    result = read(landlord, entity="ledger_entry", group_by="entry_type")
    assert all("count" in row for row in result["groups"])


# -------------------------------------------------- the invariants must hold

def test_scope_still_wins(landlord, other_landlord, august):
    """Aggregation must never become a way to total somebody else's books."""
    assert read(
        other_landlord, entity="ledger_entry", aggregate="count, sum:amount",
    )["totals"]["count"] == 0


def test_an_undeclared_field_cannot_be_aggregated(landlord):
    result = read(landlord, entity="ledger_entry", aggregate="sum:secret_margin")
    assert "Can't aggregate on 'secret_margin'" in result["error"]
    # The error names what IS allowed — the model's only documentation at runtime.
    assert "amount" in result["error"]


def test_a_non_quantity_cannot_be_aggregated(landlord):
    result = read(landlord, entity="ledger_entry", aggregate="sum:entry_type")
    assert "Can't aggregate on 'entry_type'" in result["error"]


def test_free_text_cannot_be_grouped(landlord):
    """One group per row is a listing wearing a summary's clothes."""
    result = read(landlord, entity="ledger_entry", group_by="reference_number")
    assert "Can't group by 'reference_number'" in result["error"]
    assert "charge_state" in result["error"]


def test_an_unknown_aggregate_function_is_refused(landlord):
    result = read(landlord, entity="ledger_entry", aggregate="median:amount")
    assert "median" in result["error"]


def test_too_many_group_keys_are_refused(landlord):
    result = read(
        landlord, entity="ledger_entry",
        group_by="entry_type, charge_state, is_damage_claim",
    )
    assert "at most two" in result["error"]


def test_a_grouping_that_explodes_is_refused_not_truncated(
    landlord, bc_lease, monkeypatch,
):
    """A cut-off group table is indistinguishable from a complete one.

    Refusing is the only honest option: a summary that silently stops at 50
    rows is read as the whole picture, and the model has no way to know.
    """
    from rentium.rama import domain_read

    _charge(landlord, bc_lease, "100.00", date(2026, 6, 1))
    _charge(landlord, bc_lease, "100.00", date(2026, 7, 1))
    _charge(landlord, bc_lease, "100.00", date(2026, 8, 1))
    monkeypatch.setattr(domain_read, "_MAX_GROUPS", 2)

    result = read(landlord, entity="ledger_entry", group_by="month:due_date")

    assert result["distinct_groups"] == 3
    assert "listing rather than a summary" in result["error"]
    # Crucially: no partial table alongside the error.
    assert "groups" not in result


def test_a_bad_month_is_explained_not_guessed(landlord):
    result = read(landlord, entity="ledger_entry", month="august")
    assert "YYYY-MM" in result["error"]


def test_period_needs_a_date_field(landlord):
    """conversation has no date_field; saying so beats filtering on nothing."""
    result = read(landlord, entity="conversation", month="2026-08")
    assert "no date field" in result["error"]


# ---------------------------------------------------------- null / empty ops

def test_is_empty_and_is_set(landlord, bc_property):
    """The one place emptiness is real: an expense that has not cleared the bank.

    Note this is the ONLY field where a null test means anything here — a rent
    charge's paid_on is always empty by validation, which is exactly why
    paid-versus-due had to become charge_state instead.
    """
    cleared, _ = ledger_services.post_expense(
        landlord=landlord, amount="120.00", category="MAINTENANCE",
        description="cleared", property=bc_property,
        incurred_date=AUG, paid_on=AUG,
    )
    pending, _ = ledger_services.post_expense(
        landlord=landlord, amount="80.00", category="SUPPLIES",
        description="not yet taken", property=bc_property, incurred_date=AUG,
    )

    empty = read(
        landlord, entity="ledger_entry",
        filters="entry_type=EXPENSE, paid_on is empty", limit="50",
    )
    assert empty["total_matched"] == 1
    assert empty["rows"][0]["amount"] == "80.00"

    given = read(
        landlord, entity="ledger_entry",
        filters="entry_type=EXPENSE, paid_on is set", limit="50",
    )
    assert given["total_matched"] == 1
    assert given["rows"][0]["amount"] == "120.00"
    assert cleared.paid_on and pending.paid_on is None


def test_emptiness_on_an_undeclared_field_is_refused(landlord):
    result = read(landlord, entity="ledger_entry", filters="nonsense is empty")
    assert "Can't test 'nonsense'" in result["error"]


def test_a_plain_read_is_unchanged(landlord, august):
    """No aggregate, no group: the original contract, intact."""
    result = read(landlord, entity="ledger_entry", limit="5")
    assert "rows" in result
    assert "fields" in result
    assert "totals" not in result
    assert result["returned"] <= 5
