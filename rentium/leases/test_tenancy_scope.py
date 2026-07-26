"""Legal regime follows what a lease COVERS, not how the unit is offered.

BC framing this encodes: a complete unit let to one party is an RTB tenancy;
separate rooms let to different parties AND shared with the landlord is a
roommate arrangement under the RTA s.4(c) exemption. The deciding facts are
lease scope and landlord sharing — rental_mode is a marketing decision and
must not move the legal answer on its own.
"""

from datetime import date

import pytest

from rentium.leases.models import Lease
from rentium.leases.tenancy_rules import landlord_shares_common_areas
from rentium.leases.tenancy_rules import lease_covers_whole_unit
from rentium.leases.tenancy_rules import rules_for_lease
from rentium.properties.models import Property
from rentium.properties.models import PropertyArea
from rentium.properties.models import PropertyGroup
from rentium.properties.models import PropertyHolding
from rentium.properties.models import PropertyUnit

pytestmark = pytest.mark.django_db


@pytest.fixture
def unit(landlord):
    holding = PropertyHolding.objects.create(
        landlord=landlord,
        name="McCaughey House",
        address="5654 McCaughey Street",
        city="Victoria",
    )
    return PropertyUnit.objects.create(
        landlord=landlord, holding=holding, name="Main Floor"
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


def _lease(landlord, **kwargs):
    """Room/group leases must use the Standard Roommate Agreement; only
    complete-unit leases may use a residential type (Lease.clean enforces it)."""
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
        status=Lease.LeaseStatus.ACTIVE,
        start_date=date.today(),
        is_month_to_month=True,
        total_rent="1800.00",
        **kwargs,
    )


# --------------------------------------------------------------- whole unit
def test_whole_unit_lease_is_rtb_even_with_landlord_shared_area_on_the_unit(
    landlord, unit
):
    """A self-contained floor let to one party stays under the Act.

    The kitchen inside that floor is the household's, not something they share
    with the owner — so an area flag alone must not knock the tenancy out of
    the RTA. Only the lease's own signed clause can do that.
    """
    listing = _listing(
        landlord,
        unit,
        "McCaughey Main Floor",
        Property.PropertyCategory.COMPLETE_UNIT,
        unit_type=Property.UnitType.MAIN_FLOOR,
        bedrooms=3,
    )
    PropertyArea.objects.create(
        unit=unit,
        name="Kitchen",
        area_type=PropertyArea.AreaType.KITCHEN,
        shared_with_landlord=False,
    )
    lease = _lease(landlord, property=listing)

    rules = rules_for_lease(lease)
    assert lease_covers_whole_unit(lease) is True
    assert rules.covers_whole_unit is True
    assert rules.rta_applies is True
    assert rules.code == "BC_RTA"


def test_a_complete_unit_lease_cannot_even_declare_landlord_sharing(landlord, unit):
    """The model already forbids the contradiction, so the rules never see it.

    common_space_shared_with is restricted to roommate agreement types, and a
    residential type is the only thing a COMPLETE_UNIT listing may use. So
    "whole self-contained unit, but the owner shares the kitchen" is
    unrepresentable by construction rather than by a rule in this module.
    """
    from django.core.exceptions import ValidationError

    listing = _listing(
        landlord,
        unit,
        "McCaughey Main Floor",
        Property.PropertyCategory.COMPLETE_UNIT,
        unit_type=Property.UnitType.MAIN_FLOOR,
    )
    with pytest.raises(ValidationError):
        _lease(landlord, property=listing, common_space_shared_with=["LANDLORD"])


# -------------------------------------------------------------- room letting
def test_room_lease_with_landlord_shared_kitchen_is_exempt(landlord, unit):
    """The roommate case: separate rooms, owner in the kitchen -> not the Act."""
    unit.rental_mode = PropertyUnit.RentalMode.BY_ROOM
    unit.save()
    group = PropertyGroup.objects.create(
        landlord=landlord, unit=unit, name="Main Floor Rooms"
    )
    room = _listing(
        landlord,
        unit,
        "Room 2",
        Property.PropertyCategory.ROOM,
        room_type=Property.RoomType.PRIVATE,
        group=group,
    )
    PropertyArea.objects.create(
        group=group,
        name="Kitchen",
        area_type=PropertyArea.AreaType.KITCHEN,
        shared_with_landlord=True,
    )
    lease = _lease(landlord, property=room)

    rules = rules_for_lease(lease)
    assert lease_covers_whole_unit(lease) is False
    assert rules.landlord_shares is True
    assert rules.rta_applies is False
    assert rules.code == "BC_EXEMPT_SHARED_WITH_LANDLORD"


def test_landlord_sharing_recorded_on_the_unit_is_still_found(landlord, unit):
    """Regression guard for the migration.

    Once a floor's layout moves onto its PropertyUnit, a landlord-shared
    kitchen recorded there must still reach the rules resolver. If it doesn't,
    an exemption silently stops being applied and the tenancy is governed by
    the wrong rulebook.
    """
    unit.rental_mode = PropertyUnit.RentalMode.BY_ROOM
    unit.save()
    room = _listing(
        landlord,
        unit,
        "Room 3",
        Property.PropertyCategory.ROOM,
        room_type=Property.RoomType.PRIVATE,
    )
    PropertyArea.objects.create(
        unit=unit,
        name="Kitchen",
        area_type=PropertyArea.AreaType.KITCHEN,
        shared_with_landlord=True,
    )
    lease = _lease(landlord, property=room)

    assert landlord_shares_common_areas(lease) is True
    assert rules_for_lease(lease).rta_applies is False


# ------------------------------------------- one party holding every room
def test_one_party_holding_every_room_is_a_whole_unit_tenancy(landlord, unit):
    """Your case: a by-room floor where one party takes the lot.

    The offering was room-by-room, but the tenancy that resulted covers the
    whole unit — and the law follows the tenancy.
    """
    unit.rental_mode = PropertyUnit.RentalMode.BY_ROOM
    unit.save()
    group = PropertyGroup.objects.create(
        landlord=landlord, unit=unit, name="Main Floor Rooms"
    )
    rooms = [
        _listing(
            landlord,
            unit,
            f"Room {n}",
            Property.PropertyCategory.ROOM,
            room_type=Property.RoomType.PRIVATE,
            group=group,
        )
        for n in (1, 2, 3)
    ]
    lease = _lease(landlord, group=group)
    for room in rooms:
        lease.lease_tenants.create(
            room=room, rent_amount="600.00", invited_email=f"t{room.name.replace(' ', '').lower()}@example.com"
        )

    assert lease_covers_whole_unit(lease) is True
    assert rules_for_lease(lease).covers_whole_unit is True


def test_party_holding_only_some_rooms_is_not_a_whole_unit_tenancy(landlord, unit):
    unit.rental_mode = PropertyUnit.RentalMode.BY_ROOM
    unit.save()
    group = PropertyGroup.objects.create(
        landlord=landlord, unit=unit, name="Main Floor Rooms"
    )
    rooms = [
        _listing(
            landlord,
            unit,
            f"Room {n}",
            Property.PropertyCategory.ROOM,
            room_type=Property.RoomType.PRIVATE,
            group=group,
        )
        for n in (1, 2, 3)
    ]
    lease = _lease(landlord, group=group)
    lease.lease_tenants.create(
        room=rooms[0], rent_amount="600.00", invited_email="only@example.com"
    )

    assert lease_covers_whole_unit(lease) is False


def test_rental_mode_alone_does_not_change_the_legal_answer(landlord, unit):
    """Marketing decisions must not move the rulebook.

    Same listing, same facts, both rental modes -> same regime. Only scope and
    landlord sharing are allowed to matter.
    """
    listing = _listing(
        landlord,
        unit,
        "McCaughey Main Floor",
        Property.PropertyCategory.COMPLETE_UNIT,
        unit_type=Property.UnitType.MAIN_FLOOR,
    )
    lease = _lease(landlord, property=listing)

    whole = rules_for_lease(lease)
    unit.rental_mode = PropertyUnit.RentalMode.BY_ROOM
    unit.save()
    by_room = rules_for_lease(lease)

    assert whole.code == by_room.code
    assert whole.rta_applies == by_room.rta_applies
