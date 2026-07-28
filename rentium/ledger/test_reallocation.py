"""
Reallocating an expense to the scope it actually belongs to.

Written from a real incident. A $19.78 hot water knob was booked against Room C
and then reallocated to the address, because the shower it fixed serves Rooms C,
D and F. There was no tool for that, so it was done by hand: void through one
API, post a fresh expense through another. Three rows, nothing linking the
replacement to what it replaced, and no reason recorded anywhere a tax summary
or a dispute could find it.

The failure mode these tests pin down is not the arithmetic — the totals were
right the whole time — but the fact that a correction had to be improvised out
of primitives, and improvisation leaves no audit trail.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest
from django.core.exceptions import ValidationError
from django.db.models import Sum

from rentium.ledger import services
from rentium.ledger.models import EntryType, LedgerEntry

pytestmark = pytest.mark.django_db


def _holding(landlord, name="950 McKenzie Ave"):
    from rentium.properties.models import PropertyHolding

    return PropertyHolding.objects.create(
        landlord=landlord, name=name, address=name, city="Victoria"
    )


def _room(landlord, holding, name="Room C"):
    from rentium.properties.models import Property

    return Property.objects.create(
        landlord=landlord,
        holding=holding,
        name=name,
        address=holding.address,
        city="Victoria",
        province="BC",
        postal_code="V8V 1V1",
        property_category=Property.PropertyCategory.ROOM,
    )


def _knob(landlord, prop, **kw):
    entry, _ = services.post_expense(
        landlord=landlord,
        amount="19.78",
        category="MAINTENANCE",
        description="Hot water knob replacement (shower leak repair)",
        property=prop,
        **kw,
    )
    return entry


SHARED = "Shared-space repair: the shower serves Rooms C, D and F"


def test_reallocating_a_room_expense_to_the_address(landlord):
    holding = _holding(landlord)
    room = _room(landlord, holding)
    original = _knob(landlord, room)

    replacement = services.reallocate_entry(
        original, property=None, holding=holding, reason=SHARED
    )

    assert replacement.property_id is None
    assert replacement.holding_id == holding.pk
    assert replacement.amount == Decimal("19.78")
    original.refresh_from_db()
    assert original.voided is True


def test_the_replacement_names_what_it_replaced(landlord):
    """The whole point of a tool over improvisation: the link survives."""
    holding = _holding(landlord)
    room = _room(landlord, holding)
    original = _knob(landlord, room)

    replacement = services.reallocate_entry(
        original, property=None, holding=holding, reason=SHARED
    )

    assert replacement.metadata["corrects"] == str(original.id)
    moved = replacement.metadata["reallocated"]
    assert moved["from"]["property_name"] == "Room C"
    assert moved["to"]["holding_name"] == "950 McKenzie Ave"
    assert moved["to"]["property_id"] is None
    assert moved["reason"] == SHARED
    assert moved["on"] == date.today().isoformat()


def test_moving_to_a_listing_derives_its_holding(landlord):
    """LedgerEntry.clean() rejects a property whose holding disagrees with the
    holding field. Normalizing that pair is why this is a service and not a
    keyword argument callers pass by hand."""
    holding = _holding(landlord)
    room = _room(landlord, holding)
    original, _ = services.post_expense(
        landlord=landlord,
        amount="19.78",
        category="MAINTENANCE",
        description="Hot water knob replacement",
        holding=holding,
    )

    replacement = services.reallocate_entry(original, property=room, reason="Room only")

    assert replacement.property_id == room.pk
    assert replacement.holding_id == holding.pk  # derived, not left stale


def test_the_work_order_link_and_bank_date_survive(landlord):
    """The damage-recovery claim is discriminated by FEE_CHARGE + work_order,
    so losing the link here would quietly unfile the claim."""
    from rentium.maintenance.models import WorkOrder

    holding = _holding(landlord)
    room = _room(landlord, holding)
    work_order = WorkOrder.objects.create(
        property=room,
        title="Shower leak + hot water knob replacement",
        category=WorkOrder.Category.PLUMBING,
    )
    original = _knob(landlord, room, work_order=work_order, paid_on=date.today())

    replacement = services.reallocate_entry(
        original, property=None, holding=holding, reason=SHARED
    )

    assert replacement.work_order_id == work_order.pk
    assert replacement.paid_on == date.today()
    assert replacement.bank_status == "PAID"


def test_the_live_expense_total_is_not_doubled(landlord):
    """The symptom a landlord would actually notice."""
    holding = _holding(landlord)
    room = _room(landlord, holding)
    services.reallocate_entry(
        _knob(landlord, room), property=None, holding=holding, reason=SHARED
    )

    live = (
        LedgerEntry.objects.filter(landlord=landlord, entry_type=EntryType.EXPENSE)
        .not_voided()
        .aggregate(t=Sum("amount"))["t"]
    )
    assert live == Decimal("19.78")


def test_nothing_is_ever_deleted(landlord):
    holding = _holding(landlord)
    room = _room(landlord, holding)
    original = _knob(landlord, room)
    before = LedgerEntry.objects.count()

    services.reallocate_entry(
        original, property=None, holding=holding, reason=SHARED
    )

    # The original, its REVERSAL, and the replacement.
    assert LedgerEntry.objects.count() == before + 2
    with pytest.raises(ValidationError):
        original.delete()


def test_reallocating_twice_is_refused(landlord):
    """The guard against a third posting — the thing improvisation had none of."""
    holding = _holding(landlord)
    room = _room(landlord, holding)
    original = _knob(landlord, room)
    services.reallocate_entry(
        original, property=None, holding=holding, reason=SHARED
    )

    with pytest.raises(services.LedgerError, match="already been voided"):
        services.reallocate_entry(
            original, property=None, holding=holding, reason=SHARED
        )


def test_a_no_op_reallocation_is_refused(landlord):
    holding = _holding(landlord)
    room = _room(landlord, holding)
    original = _knob(landlord, room)

    with pytest.raises(services.LedgerError, match="already booked there"):
        services.reallocate_entry(original, property=room, reason=SHARED)


def test_a_charge_cannot_be_reallocated(landlord, bc_lease, bc_property):
    charge, _ = services.post_charge(
        landlord=landlord,
        tenant=None,
        lease=bc_lease,
        property=bc_property,
        amount="850.00",
        due_date=date.today(),
        entry_type=EntryType.RENT_CHARGE,
        description="Monthly rent",
    )

    with pytest.raises(services.LedgerError, match="Only an expense"):
        services.reallocate_entry(charge, property=None, reason="Wrong room")
