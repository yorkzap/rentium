"""
Recording money IN.

Until this tool existed RAMA had 115 tools and not one could record that a
payment arrived — it could only ever spend. Asked to record $100 of a $425
deposit it had nothing to reach for, and said "Recorded the $100 payment"
anyway.

The scenario in every test below is that real one: a $425 deposit charge, of
which $100 has actually been received.
"""

from __future__ import annotations

import datetime
from decimal import Decimal

import pytest

from rentium.ledger.models import EntryType, LedgerEntry
from rentium.rama import registry

pytestmark = pytest.mark.django_db

TODAY = datetime.date.today()


@pytest.fixture
def deposit(landlord, bc_lease):
    """The $425 deposit charge, unpaid, overdue."""
    from rentium.ledger import services as ledger_services

    charge, _ = ledger_services.post_charge(
        landlord=landlord,
        property=bc_lease.property,
        lease=bc_lease,
        tenant=None,
        entry_type=EntryType.DEPOSIT_CHARGE,
        amount=Decimal("425.00"),
        due_date=TODAY - datetime.timedelta(days=6),
        description="Security deposit — due on signing",
    )
    return charge


def _call(landlord, **kwargs):
    return registry.execute("record_payment", kwargs, landlord=landlord)


# ------------------------------------------------------------ the preview
def test_a_partial_payment_previews_the_balance_that_follows(landlord, deposit):
    """The number the landlord actually wants before saying yes — "$100 of
    $425, so $325 left" — rather than just echoing back what they typed."""
    result = _call(landlord, amount="100.00", charge_query="deposit", payment_method="etransfer")

    assert result["needs_confirm"] is True
    preview = result["preview"]
    assert preview["this_payment"] == "100.00"
    assert preview["still_owing_after"] == "325.00"
    assert preview["charge_amount"] == "425.00"


def test_previewing_writes_nothing(landlord, deposit):
    _call(landlord, amount="100.00", charge_query="deposit", payment_method="etransfer")
    assert not LedgerEntry.objects.filter(entry_type=EntryType.PAYMENT).exists()


def test_confirming_posts_the_payment(landlord, deposit):
    result = _call(
        landlord, amount="100.00", charge_query="deposit", payment_method="etransfer", confirm="yes"
    )

    assert result["ok"] is True
    assert result["still_owing"] == "325.00"
    payment = LedgerEntry.objects.get(entry_type=EntryType.PAYMENT)
    assert payment.amount == Decimal("100.00")
    assert payment.settles_id == deposit.pk


def test_the_charge_becomes_partially_paid_not_paid(landlord, deposit):
    """The whole point of the bug: $100 against $425 is progress, not
    settlement, and the ledger has to keep saying so."""
    _call(landlord, amount="100.00", charge_query="deposit", payment_method="etransfer", confirm="yes")
    deposit.refresh_from_db()
    assert deposit.charge_status() == "PARTIALLY_PAID"


def test_paying_the_rest_settles_it(landlord, deposit):
    _call(landlord, amount="100.00", charge_query="deposit", payment_method="etransfer", confirm="yes")
    result = _call(
        landlord, amount="325.00", charge_query="deposit", payment_method="etransfer", confirm="yes"
    )
    assert result["still_owing"] == "0.00"
    deposit.refresh_from_db()
    assert deposit.charge_status() == "PAID"


def test_the_second_preview_knows_about_the_first_payment(landlord, deposit):
    _call(landlord, amount="100.00", charge_query="deposit", payment_method="etransfer", confirm="yes")
    preview = _call(landlord, amount="325.00", charge_query="deposit", payment_method="etransfer")["preview"]
    assert preview["already_paid"] == "100.00"
    assert preview["still_owing_after"] == "0.00"


# ------------------------------------------------------- what it refuses
def test_a_payment_with_no_charge_is_refused(landlord):
    """Money is recorded against the charge it settles — never free-floating,
    or the ledger stops reconciling."""
    result = _call(landlord, amount="100.00", charge_query="deposit", payment_method="etransfer")
    assert result["error"] == "no_matching_charge"


def test_an_ambiguous_charge_asks_rather_than_guessing(landlord, bc_lease):
    from rentium.ledger import services as ledger_services

    for month in ("August", "September"):
        ledger_services.post_charge(
            landlord=landlord, property=bc_lease.property, lease=bc_lease,
            tenant=None, entry_type=EntryType.RENT_CHARGE,
            amount=Decimal("850.00"), due_date=TODAY,
            description=f"Monthly rent — {month}",
        )
    result = _call(landlord, amount="850.00", charge_query="rent", payment_method="etransfer")
    assert "question_for_user" in result
    assert len(result["candidates"]) == 2


def test_an_overpayment_is_flagged_but_still_possible(landlord, deposit):
    """Tenants do overpay. Warn loudly, don't refuse."""
    preview = _call(landlord, amount="500.00", charge_query="deposit", payment_method="etransfer")["preview"]
    assert "425.00 still" in preview["overpayment_warning"]
    assert preview["still_owing_after"] == "0.00"


def test_a_negative_payment_is_refused(landlord, deposit):
    assert "positive" in _call(landlord, amount="-100", charge_query="deposit", payment_method="etransfer")["error"]


def test_confirming_twice_does_not_double_record(landlord, deposit):
    """The landlord says yes, the reply is slow, they say yes again. Two
    $100 payments would make a $225 balance out of a $325 one."""
    _call(landlord, amount="100.00", charge_query="deposit", payment_method="etransfer", confirm="yes")
    second = _call(
        landlord, amount="100.00", charge_query="deposit", payment_method="etransfer", confirm="yes"
    )
    assert second["duplicate"] is True
    assert LedgerEntry.objects.filter(entry_type=EntryType.PAYMENT).count() == 1
    deposit.refresh_from_db()
    assert deposit.charge_status() == "PARTIALLY_PAID"


def test_a_voided_charge_takes_no_payment(landlord, deposit):
    from rentium.ledger import services as ledger_services

    ledger_services.void_entry(deposit, reason="posted in error")
    result = _call(
        landlord, amount="100.00", charge_query="deposit", payment_method="etransfer", confirm="yes"
    )
    assert "error" in result


def test_another_landlord_cannot_pay_this_charge(landlord, deposit):
    from rentium.users.models import LandlordProfile
    from rentium.users.tests.factories import UserFactory

    stranger = LandlordProfile.objects.create(user=UserFactory())
    result = _call(stranger, amount="100.00", charge_query="deposit", payment_method="etransfer")
    assert result["error"] == "no_matching_charge"


# ------------------------------------------- what the dashboard then shows
def test_the_deposit_shows_as_held_once_received(landlord, deposit):
    """"Deposits held $0.00" was right — the money was never recorded. Once
    it is, it has to appear."""
    from rentium.ledger import services as ledger_services

    assert ledger_services.deposits_held(landlord) == Decimal("0.00")
    _call(landlord, amount="100.00", charge_query="deposit", payment_method="etransfer", confirm="yes")
    assert ledger_services.deposits_held(landlord) == Decimal("100.00")


def test_the_tool_is_reachable_and_needs_a_confirmation(landlord):
    from rentium.rama.tool_meta import Autonomy, meta_for

    assert "record_payment" in registry.REGISTRY
    assert "confirm" in registry.REGISTRY["record_payment"].parameters["properties"]
    # Never unattended: an invented payment makes a debt vanish, and unlike an
    # expense there is nobody on the other side to notice.
    assert meta_for("record_payment").autonomy == Autonomy.NEVER


def test_the_treasurer_cannot_record_a_payment():
    from rentium.rama.roles import TREASURER_TOOLS

    assert "record_payment" not in TREASURER_TOOLS


# =================================== what the dashboard says once it lands
def test_a_deposit_payment_shows_up_as_received_not_as_income(landlord, deposit):
    """The trap behind "Collected this month $0.00 of $0.00 expected".

    A deposit is a refundable liability, so it correctly stays OUT of income —
    but the money genuinely hit the bank, and a landlord who has just banked
    $100 must not read "$0.00 collected". The API carries both numbers.
    """
    from rest_framework.test import APIClient

    client = APIClient()
    client.force_authenticate(user=landlord.user)

    _call(landlord, amount="100.00", charge_query="deposit", payment_method="etransfer", confirm="yes")
    body = client.get("/api/ledger/summary/").json()
    current = body["monthly"][-1]

    # Accounting stays honest...
    assert current["collected_income"] == "0.00"
    # ...and the landlord still sees their $100.
    assert current["deposits_collected"] == "100.00"
    assert body["collected_this_month_total"] == "100.00"
    assert body["deposits_held"] == "100.00"


def test_the_outstanding_deposit_drops_by_what_was_paid(landlord, deposit):
    from rest_framework.test import APIClient

    client = APIClient()
    client.force_authenticate(user=landlord.user)

    before = client.get("/api/ledger/summary/").json()
    assert before["deposits_outstanding"] == "425.00"

    _call(landlord, amount="100.00", charge_query="deposit", payment_method="etransfer", confirm="yes")

    after = client.get("/api/ledger/summary/").json()
    assert after["deposits_outstanding"] == "325.00"
    # It is still owed, so it still counts — one partial payment does not
    # make a charge disappear from the tile.
    assert after["deposits_overdue_count"] == 1


def test_the_payment_is_dated_when_the_money_arrived(landlord, deposit):
    """Not when the landlord got round to telling RAMA. A deposit's date
    starts the BC return clock and its interest, so "today" is a guess with
    legal consequences."""
    import datetime

    arrived = datetime.date.today() - datetime.timedelta(days=5)
    _call(
        landlord,
        amount="100.00",
        charge_query="deposit",
        payment_method="etransfer",
        payment_date=arrived.isoformat(),
        confirm="yes",
    )
    payment = LedgerEntry.objects.get(entry_type=EntryType.PAYMENT)
    assert payment.effective_date == arrived


# ------------------------------------- what it will not put on the record
def test_an_unstated_method_is_asked_for_not_guessed(landlord, deposit):
    """A silent "e-Transfer" default writes a fact nobody stated onto a
    financial record. The landlord finds out when they reconcile against a
    statement that says cash."""
    result = _call(landlord, amount="100.00", charge_query="deposit")
    assert result["needs"] == "payment_method"
    assert "e-transfer, cash, or cheque" in result["question_for_user"]
    assert not LedgerEntry.objects.filter(entry_type=EntryType.PAYMENT).exists()


def test_an_unrecognised_method_is_asked_about(landlord, deposit):
    result = _call(
        landlord, amount="100.00", charge_query="deposit", payment_method="bitcoin"
    )
    assert result["needs"] == "payment_method"


@pytest.mark.parametrize(
    "said,stored",
    [
        ("etransfer", "ETRANSFER"), ("e-transfer", "ETRANSFER"),
        ("Interac", "ETRANSFER"), ("cash", "CASH"), ("cheque", "CHEQUE"),
        ("check", "CHEQUE"),
    ],
)
def test_how_the_landlord_says_it_is_understood(landlord, deposit, said, stored):
    _call(
        landlord, amount="100.00", charge_query="deposit",
        payment_method=said, confirm="yes",
    )
    assert LedgerEntry.objects.get(entry_type=EntryType.PAYMENT).payment_method == stored


def test_no_date_means_the_day_they_told_us(landlord, deposit):
    """When the landlord doesn't name a date, the honest stamp is the day they
    reported it — not a date the tool invented."""
    import datetime

    _call(
        landlord, amount="100.00", charge_query="deposit",
        payment_method="etransfer", confirm="yes",
    )
    payment = LedgerEntry.objects.get(entry_type=EntryType.PAYMENT)
    assert payment.effective_date == datetime.date.today()


def test_one_etransfer_splits_security_and_cleaning_deposits(landlord, bc_lease):
    from rentium.ledger import services as ledger_services

    charges = []
    for description, kind in (
        ("Security deposit — due on signing", "security_deposit"),
        ("Cleaning deposit — due on signing", "cleaning_deposit_lease"),
    ):
        charge, _ = ledger_services.post_charge(
            landlord=landlord,
            property=bc_lease.property,
            lease=bc_lease,
            tenant=None,
            entry_type=EntryType.DEPOSIT_CHARGE,
            amount=Decimal("200.00"),
            due_date=TODAY,
            description=description,
            metadata={"kind": kind},
        )
        charges.append(charge)

    preview = _call(
        landlord,
        amount="400",
        charge_query="deposit",
        payment_method="etransfer",
    )
    # Relayed under record_payment, NOT under the internal helper that built
    # it: record_payment_allocation is not in the registry, so "call
    # record_payment_allocation again with confirm=yes" named a tool the model
    # cannot call, and the landlord's yes had nowhere to land.
    assert preview["action"] == "record_payment"
    assert "record_payment_allocation" not in preview["instruction"]
    assert {row["payment"] for row in preview["preview"]["allocations"]} == {
        "200.00"
    }

    done = _call(
        landlord,
        amount="400",
        charge_query="deposit",
        payment_method="etransfer",
        confirm="yes",
    )
    assert done.get("ok"), done
    payments = list(LedgerEntry.objects.filter(entry_type=EntryType.PAYMENT))
    assert len(payments) == 2
    assert {payment.settles_id for payment in payments} == {
        charge.pk for charge in charges
    }
    assert sum((payment.amount for payment in payments), Decimal("0")) == Decimal(
        "400.00"
    )
