"""PropertyUnit: the physical space, separate from how it is rented.

These tests pin the distinction the old model could not express — that
"McCaughey Main Floor" is ONE unit containing three bedrooms, not three
rentable rooms — and that a listing parked by a rental-mode switch stops
being advertised without being destroyed.
"""

import pytest
from django.db.utils import IntegrityError

from rentium.properties.models import Property
from rentium.properties.models import PropertyGroup
from rentium.properties.models import PropertyHolding
from rentium.properties.models import PropertyUnit

pytestmark = pytest.mark.django_db


@pytest.fixture
def holding(landlord):
    return PropertyHolding.objects.create(
        landlord=landlord,
        name="McCaughey House",
        address="5654 McCaughey Street",
        city="Regina",
    )


@pytest.fixture
def main_floor(landlord, holding):
    return PropertyUnit.objects.create(
        landlord=landlord,
        holding=holding,
        name="Main Floor",
        unit_type=PropertyUnit.UnitType.MAIN_FLOOR,
        rental_mode=PropertyUnit.RentalMode.WHOLE_UNIT,
        layout_complete=True,
    )


def _listing(landlord, unit, name, category, **kwargs):
    return Property.objects.create(
        landlord=landlord,
        unit=unit,
        holding=unit.holding,
        name=name,
        address=unit.holding.address,
        city=unit.holding.city,
        province="sk",
        property_category=category,
        **kwargs,
    )


def test_a_floor_is_one_unit_regardless_of_bedroom_count(landlord, main_floor):
    """The whole point: bedrooms live inside the unit, they are not offerings."""
    offering = _listing(
        landlord,
        main_floor,
        "McCaughey Main Floor",
        Property.PropertyCategory.COMPLETE_UNIT,
        unit_type=Property.UnitType.MAIN_FLOOR,
        bedrooms=3,
    )
    assert main_floor.offerings.count() == 1
    assert offering.bedrooms == 3
    assert main_floor.is_whole_unit


def test_unit_names_are_unique_per_holding_not_globally(landlord, holding):
    """Two houses may both have a "Main Floor"; one house may not."""
    PropertyUnit.objects.create(landlord=landlord, holding=holding, name="Main Floor")

    other = PropertyHolding.objects.create(
        landlord=landlord, name="Wascana House", address="3213 Wascana St", city="Victoria"
    )
    PropertyUnit.objects.create(landlord=landlord, holding=other, name="Main Floor")

    with pytest.raises(IntegrityError):
        PropertyUnit.objects.create(
            landlord=landlord, holding=holding, name="Main Floor"
        )


def test_room_group_is_one_to_one_with_its_unit(landlord, holding):
    """A group now means exactly one thing: this unit is let room by room."""
    unit = PropertyUnit.objects.create(
        landlord=landlord,
        holding=holding,
        name="Upstairs",
        rental_mode=PropertyUnit.RentalMode.BY_ROOM,
    )
    group = PropertyGroup.objects.create(
        landlord=landlord, unit=unit, name="Upstairs Rooms"
    )
    assert unit.room_group == group


def test_active_offerings_excludes_parked_listings(landlord, main_floor):
    """Switching mode parks the other mode's listings; it never deletes them."""
    live = _listing(
        landlord,
        main_floor,
        "McCaughey Main Floor",
        Property.PropertyCategory.COMPLETE_UNIT,
        unit_type=Property.UnitType.MAIN_FLOOR,
    )
    parked = _listing(
        landlord,
        main_floor,
        "McCaughey Room 2",
        Property.PropertyCategory.ROOM,
        room_type=Property.RoomType.PRIVATE,
        is_active_offering=False,
    )

    assert list(main_floor.active_offerings()) == [live]
    # Parked, not gone — switching back must be able to reuse it.
    assert parked.pk is not None
    assert main_floor.offerings.count() == 2


def test_public_queryset_hides_parked_offerings(landlord, main_floor):
    """The three rooms of a floor now rented whole must stop being advertised.

    Guards THE visibility rule (PropertyQuerySet.public) — the single place
    every public page, the sitemap and the showcase go through.
    """
    from rentium.showcase.models import Showcase

    Showcase.objects.update_or_create(
        landlord=landlord, defaults={"is_public": True, "slug": "mccaughey-rentals"}
    )

    live = _listing(
        landlord,
        main_floor,
        "McCaughey Main Floor",
        Property.PropertyCategory.COMPLETE_UNIT,
        unit_type=Property.UnitType.MAIN_FLOOR,
    )
    _listing(
        landlord,
        main_floor,
        "McCaughey Room 2",
        Property.PropertyCategory.ROOM,
        room_type=Property.RoomType.PRIVATE,
        is_active_offering=False,
    )

    public = list(Property.objects.public())
    assert public == [live]
