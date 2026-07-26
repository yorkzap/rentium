"""Switching how a unit is rented: reversible, never destructive, never
overlapping.

Three guarantees worth pinning:
  1. a switch parks the old listings instead of deleting them, and switching
     back reuses the originals;
  2. it is refused outright while any lease is live in the unit;
  3. a unit can never be let whole and by the room at the same time.
"""

from datetime import date

import pytest
from django.core.exceptions import ValidationError

from rentium.leases.models import Lease
from rentium.properties.models import Property
from rentium.properties.models import PropertyGroup
from rentium.properties.models import PropertyHolding
from rentium.properties.models import PropertyUnit
from rentium.properties.services import RentalModeError
from rentium.properties.services import describe_rental_mode_switch
from rentium.properties.services import set_rental_mode

pytestmark = pytest.mark.django_db


@pytest.fixture
def unit(landlord):
    holding = PropertyHolding.objects.create(
        landlord=landlord,
        name="Wascana House",
        address="3213 Wascana St",
        city="Victoria",
    )
    return PropertyUnit.objects.create(
        landlord=landlord,
        holding=holding,
        name="Main Floor",
        rental_mode=PropertyUnit.RentalMode.WHOLE_UNIT,
    )


def _listing(landlord, unit, name, category, **kwargs):
    return Property.objects.create(
        landlord=landlord,
        unit=unit,
        holding=unit.holding,
        name=name,
        address=unit.holding.address,
        city=unit.holding.city,
        province="bc",
        property_category=category,
        **kwargs,
    )


def _whole(landlord, unit, name="Wascana Main Floor"):
    return _listing(
        landlord,
        unit,
        name,
        Property.PropertyCategory.COMPLETE_UNIT,
        unit_type=Property.UnitType.MAIN_FLOOR,
    )


def _room(landlord, unit, name, group=None, active=True):
    return _listing(
        landlord,
        unit,
        name,
        Property.PropertyCategory.ROOM,
        room_type=Property.RoomType.PRIVATE,
        group=group,
        is_active_offering=active,
    )


def _lease(landlord, status=Lease.LeaseStatus.ACTIVE, **kwargs):
    room_scoped = "group" in kwargs or (
        kwargs.get("property") is not None
        and kwargs["property"].property_category == Property.PropertyCategory.ROOM
    )
    return Lease.objects.create(
        landlord=landlord,
        lease_type=(
            Lease.LeaseType.GENERIC_ROOMMATE
            if room_scoped
            else Lease.LeaseType.BC_RESIDENTIAL_TENANCY
        ),
        status=status,
        start_date=date.today(),
        is_month_to_month=True,
        total_rent="1800.00",
        **kwargs,
    )


# ------------------------------------------------------------------ switching
def test_switch_parks_listings_rather_than_deleting_them(landlord, unit):
    whole = _whole(landlord, unit)

    result = set_rental_mode(unit, PropertyUnit.RentalMode.BY_ROOM)

    whole.refresh_from_db()
    unit.refresh_from_db()
    assert unit.rental_mode == PropertyUnit.RentalMode.BY_ROOM
    assert whole.is_active_offering is False
    assert whole.pk is not None, "parked, not deleted"
    assert result["parked"] == ["Wascana Main Floor"]
    assert result["needs_new_listing"] is True


def test_switching_back_reuses_the_original_listing(landlord, unit):
    """The reason parking beats deleting: photos, description and history
    come back with it instead of being retyped."""
    whole = _whole(landlord, unit)
    original_pk = whole.pk

    set_rental_mode(unit, PropertyUnit.RentalMode.BY_ROOM)
    _room(landlord, unit, "Room 1")
    result = set_rental_mode(unit, PropertyUnit.RentalMode.WHOLE_UNIT)

    whole.refresh_from_db()
    assert whole.pk == original_pk
    assert whole.is_active_offering is True
    assert result["reactivated"] == ["Wascana Main Floor"]
    assert result["needs_new_listing"] is False


def test_switch_is_blocked_by_a_live_lease(landlord, unit):
    whole = _whole(landlord, unit)
    _lease(landlord, property=whole)

    with pytest.raises(RentalModeError, match="live leases"):
        set_rental_mode(unit, PropertyUnit.RentalMode.BY_ROOM)

    unit.refresh_from_db()
    whole.refresh_from_db()
    assert unit.rental_mode == PropertyUnit.RentalMode.WHOLE_UNIT
    assert whole.is_active_offering is True


@pytest.mark.parametrize(
    "status", [Lease.LeaseStatus.DRAFT, Lease.LeaseStatus.PENDING_SIGNATURES]
)
def test_draft_and_pending_leases_also_block(landlord, unit, status):
    """Paperwork someone is mid-way through counts — re-shaping the offering
    under a half-signed agreement is exactly the harm."""
    whole = _whole(landlord, unit)
    _lease(landlord, status=status, property=whole)

    with pytest.raises(RentalModeError):
        set_rental_mode(unit, PropertyUnit.RentalMode.BY_ROOM)


def test_terminated_leases_do_not_block(landlord, unit):
    whole = _whole(landlord, unit)
    lease = _lease(landlord, property=whole)
    lease.status = Lease.LeaseStatus.TERMINATED
    lease.save()

    set_rental_mode(unit, PropertyUnit.RentalMode.BY_ROOM)
    unit.refresh_from_db()
    assert unit.rental_mode == PropertyUnit.RentalMode.BY_ROOM


def test_switching_to_the_same_mode_is_refused(landlord, unit):
    _whole(landlord, unit)
    with pytest.raises(RentalModeError, match="already rented"):
        set_rental_mode(unit, PropertyUnit.RentalMode.WHOLE_UNIT)


def test_preview_reports_without_changing_anything(landlord, unit):
    whole = _whole(landlord, unit)
    _lease(landlord, property=whole)

    preview = describe_rental_mode_switch(unit, PropertyUnit.RentalMode.BY_ROOM)

    assert preview["ok"] is False
    assert preview["blocked_by"][0]["status"] == Lease.LeaseStatus.ACTIVE
    assert preview["will_park"] == ["Wascana Main Floor"]
    unit.refresh_from_db()
    whole.refresh_from_db()
    assert unit.rental_mode == PropertyUnit.RentalMode.WHOLE_UNIT
    assert whole.is_active_offering is True


# ------------------------------------------------------------ scope overlap
def test_a_room_lease_cannot_be_added_under_a_whole_unit_lease(landlord, unit):
    """Otherwise the floor is double-let: the family renting the whole place
    and the roommate renting Bedroom 2 both hold a valid agreement to it."""
    whole = _whole(landlord, unit)
    _lease(landlord, property=whole)

    room = _room(landlord, unit, "Room 2")
    with pytest.raises(ValidationError, match="whole-unit lease"):
        _lease(landlord, property=room)


def test_a_whole_unit_lease_cannot_be_added_over_a_room_lease(landlord, unit):
    unit.rental_mode = PropertyUnit.RentalMode.BY_ROOM
    unit.save()
    group = PropertyGroup.objects.create(
        landlord=landlord, unit=unit, name="Main Floor Rooms"
    )
    room = _room(landlord, unit, "Room 2", group=group)
    _lease(landlord, property=room)

    whole = _whole(landlord, unit)
    with pytest.raises(ValidationError, match="room lease"):
        _lease(landlord, property=whole)


def test_two_room_leases_on_the_same_unit_are_fine(landlord, unit):
    """The ordinary roommate case must not be caught by the overlap guard."""
    unit.rental_mode = PropertyUnit.RentalMode.BY_ROOM
    unit.save()
    group = PropertyGroup.objects.create(
        landlord=landlord, unit=unit, name="Main Floor Rooms"
    )
    room1 = _room(landlord, unit, "Room 1", group=group)
    room2 = _room(landlord, unit, "Room 2", group=group)

    _lease(landlord, property=room1)
    _lease(landlord, property=room2)  # must not raise


def test_terminated_whole_unit_lease_does_not_block_a_room_lease(landlord, unit):
    whole = _whole(landlord, unit)
    lease = _lease(landlord, property=whole)
    lease.status = Lease.LeaseStatus.TERMINATED
    lease.save()

    room = _room(landlord, unit, "Room 2")
    _lease(landlord, property=room)  # must not raise
