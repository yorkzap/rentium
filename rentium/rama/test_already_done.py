"""
A write that is already on the books must never be offered again.

THE INCIDENT
------------
The landlord recorded a $100 Room C deposit payment. RAMA then offered:

    Preview for your approval:
    Record a Treasurer fact:
    Subject: room-c-deposit-payment-received
    Fact: Received $100 cash for the Room C security deposit on 2026-07-29.
    Amount: $100.00 ... Direction: INCOME
    Confirm?

The same $100, a second time, in a second store — and with two details it had
invented (cash, when it was an e-transfer; tomorrow's date, when the payment
was today). The landlord had to notice. They did: "its done already i thought".

Three tools had hand-written duplicate checks and none of them covered this,
because a Treasurer fact and a ledger PAYMENT are different stores and nothing
compared across them. `ToolMeta.already_done` is the fix as policy rather than
as three more special cases.
"""

from __future__ import annotations

import datetime
from decimal import Decimal

import pytest

from rentium.ledger import services as ledger_services
from rentium.ledger.models import EntryType
from rentium.rama import registry
from rentium.rama.tool_meta import already_done_for

pytestmark = pytest.mark.django_db

TODAY = datetime.date.today()


@pytest.fixture
def deposit_charge(landlord, bc_lease):
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


@pytest.fixture
def hundred_received(landlord, deposit_charge):
    """The $100, on the books, by e-transfer, today."""
    entry, _ = ledger_services.record_payment(
        charge=deposit_charge,
        amount=Decimal("100.00"),
        payment_method="ETRANSFER",
        payment_date=TODAY,
        idempotency_key="already-done-hundred",
    )
    return entry


def _holding_name(lease) -> str:
    return lease.property.holding.name if lease.property.holding_id else ""


# --------------------------------------------------------- the incident
def test_a_fact_restating_a_recorded_payment_is_refused(
    landlord, bc_lease, hundred_received
):
    """The exact proposal from the transcript must not survive the gate."""
    detail = already_done_for(
        "record_treasurer_fact",
        landlord,
        subject="room-c-deposit-payment-received",
        fact="Received $100 cash for the Room C security deposit.",
        amount="100.00",
        period="ONE_TIME",
        direction="INCOME",
        holding_name=_holding_name(bc_lease),
    )

    assert detail is not None
    assert "$100.00 is already in the ledger" in detail
    # It must NAME the record, not just assert a clash — "it's a duplicate" is
    # not actionable; "it's the e-transfer on the 28th" is.
    assert "Payment Received" in detail
    assert TODAY.isoformat() in detail
    assert "e-Transfer" in detail


def test_the_refusal_replaces_the_preview_entirely(
    landlord, bc_lease, hundred_received
):
    """Not a warning buried in a preview payload the model may drop from its
    prose — the tool call comes back refused, so there is nothing to confirm."""
    result = registry.execute(
        "record_treasurer_fact",
        {
            "subject": "room-c-deposit-payment-received",
            "fact": "Received $100 for the Room C security deposit.",
            "amount": "100.00",
            "period": "ONE_TIME",
            "holding_name": _holding_name(bc_lease),
        },
        landlord=landlord,
    )
    # The tool itself still previews — the gate lives on the generic path in
    # service.run_turn, so that a NEW write tool inherits it. Assert the gate
    # fires on these arguments rather than that the tool self-censors.
    assert result.get("needs_confirm") is True
    assert already_done_for("record_treasurer_fact", landlord, **{
        "subject": "room-c-deposit-payment-received",
        "fact": "Received $100 for the Room C security deposit.",
        "amount": "100.00",
        "period": "ONE_TIME",
        "holding_name": _holding_name(bc_lease),
    })


def test_direction_cannot_switch_the_check_off(landlord, bc_lease, hundred_received):
    """`reconcile` returns no opinion at all when direction is NEUTRAL, so an
    unrecognised sentence disabled the only duplicate guard there was. This
    check must not consult direction."""
    for direction in ("", "NEUTRAL", "INCOME", "EXPENSE"):
        detail = already_done_for(
            "record_treasurer_fact",
            landlord,
            subject="some-subject",
            fact="A hundred dollars changed hands.",
            amount="100.00",
            period="ONE_TIME",
            direction=direction,
            holding_name=_holding_name(bc_lease),
        )
        assert detail is not None, f"direction={direction!r} disabled the check"


def test_an_undated_assertion_still_gets_a_window(landlord, bc_lease, hundred_received):
    """A ONE_TIME fact needs no dates, and with no dates there used to be no
    date filter at all — so the check was all-or-nothing. It must still fire."""
    detail = already_done_for(
        "record_treasurer_fact",
        landlord,
        subject="deposit-money",
        fact="We received $100 toward the deposit.",
        amount="100.00",
        period="ONE_TIME",
        holding_name=_holding_name(bc_lease),
    )
    assert detail is not None


# ------------------------------------------------- what must still get through
def test_a_genuine_gap_is_not_refused(landlord, bc_lease, hundred_received):
    """The whole point of a Treasurer fact is money the ledger LACKS. An amount
    with no matching entry must record normally, or the gate has eaten the
    feature it was protecting."""
    detail = already_done_for(
        "record_treasurer_fact",
        landlord,
        subject="cash-rent-from-basement",
        fact="We took $2,000 rent in cash from the basement tenant.",
        amount="2000.00",
        period="ONE_TIME",
        holding_name=_holding_name(bc_lease),
    )
    assert detail is None


def test_a_qualitative_fact_is_never_refused(landlord, bc_lease, hundred_received):
    """No amount, nothing to double-count."""
    detail = already_done_for(
        "record_treasurer_fact",
        landlord,
        subject="tenant-pays-late",
        fact="The upstairs tenant always pays about a week late.",
        holding_name=_holding_name(bc_lease),
    )
    assert detail is None


def test_an_unpaid_charge_is_not_evidence_the_money_arrived(landlord, deposit_charge):
    """A $425 CHARGE is a receivable, not a record that $425 turned up — so the
    refusal here must not be the "already in the ledger" one. That distinction
    is what stops `already_in_ledger` from quietly widening to charge types and
    telling a landlord their money is recorded when nothing has been received.

    The fact is still refused, for the opposite reason: the ledger is holding a
    charge this money was posted against, so it belongs in record_payment,
    where it settles the charge, starts the deposit clock and reaches
    deposits-held. See test_deposits_received.py — a Treasurer fact would have
    left all three untouched.
    """
    detail = already_done_for(
        "record_treasurer_fact",
        landlord,
        subject="deposit-received",
        fact="We received the $425 deposit in cash, off the books.",
        amount="425.00",
        period="ONE_TIME",
    )
    assert "already in the ledger" not in (detail or "")
    assert "record_payment" in detail


def test_a_holding_scoped_fact_sees_entries_written_per_property(landlord, bc_lease):
    """The scope mismatch that makes this class of miss so easy: a fact is
    scoped to a HOLDING (the address), while every ledger entry is written
    against a PROPERTY (the unit). Matching only on `holding` finds nothing.
    """
    from rentium.properties.models import PropertyHolding

    holding = PropertyHolding.objects.create(
        landlord=landlord, name="950 McKenzie Ave", address="950 McKenzie Ave"
    )
    prop = bc_lease.property
    prop.holding = holding
    prop.save(update_fields=["holding"])

    charge, _ = ledger_services.post_charge(
        landlord=landlord,
        property=prop,
        lease=bc_lease,
        tenant=None,
        entry_type=EntryType.DEPOSIT_CHARGE,
        amount=Decimal("300.00"),
        due_date=TODAY,
        description="Room C security deposit",
    )
    ledger_services.record_payment(
        charge=charge,
        amount=Decimal("100.00"),
        payment_method="ETRANSFER",
        payment_date=TODAY,
        idempotency_key="holding-scoped-hundred",
    )

    detail = already_done_for(
        "record_treasurer_fact",
        landlord,
        subject="room-c-deposit-payment-received",
        fact="Received $100 for the Room C security deposit.",
        amount="100.00",
        period="ONE_TIME",
        holding_name="950 McKenzie Ave",
    )
    assert detail is not None, "a holding-scoped fact missed a per-property entry"
    assert "950 McKenzie" in detail or prop.name in detail


def test_a_tool_without_the_hook_is_unaffected(landlord):
    """`already_done` is opt-in per tool; absence means no opinion."""
    assert already_done_for("create_work_order", landlord, title="Fix the tap") is None


def test_a_broken_check_does_not_block_the_write(landlord, monkeypatch):
    """A dedupe check that raises must mean "no opinion", never a refused
    write — the failure direction has to be toward letting the landlord work."""
    from rentium.rama import tool_meta

    def boom(landlord, **kwargs):
        raise RuntimeError("check exploded")

    monkeypatch.setitem(
        tool_meta.TOOL_META,
        "record_treasurer_fact",
        tool_meta.ToolMeta(risk="low", already_done=boom),
    )
    assert already_done_for("record_treasurer_fact", landlord, amount="100.00") is None
