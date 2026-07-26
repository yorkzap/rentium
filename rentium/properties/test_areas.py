"""PropertyArea after absorbing the retired `Area` model.

Two things worth pinning: an area can hang off a unit (so a floor rented whole
still records its bedrooms), and auto-seeded placeholders stay distinguishable
from layout the landlord actually told us about.
"""

import pytest
from django.core.exceptions import ValidationError

from rentium.properties.areas import areas_for_tenant_room
from rentium.properties.areas import seed_default_areas
from rentium.properties.models import Property
from rentium.properties.models import PropertyArea
from rentium.properties.models import PropertyGroup
from rentium.properties.models import PropertyHolding
from rentium.properties.models import PropertyUnit

pytestmark = pytest.mark.django_db


@pytest.fixture
def unit(landlord):
    holding = PropertyHolding.objects.create(
        landlord=landlord, name="Wascana House", address="3213 Wascana St", city="Victoria"
    )
    return PropertyUnit.objects.create(
        landlord=landlord, holding=holding, name="Main Floor"
    )


def test_a_whole_unit_records_its_bedrooms_as_areas(landlord, unit):
    """The core capability the old model lacked: bedrooms exist inside a unit
    without being rentable listings."""
    master = PropertyArea.objects.create(
        unit=unit,
        name="Master Bedroom",
        area_type=PropertyArea.AreaType.BEDROOM,
        kind=PropertyArea.Kind.PRIVATE,
    )
    ensuite = PropertyArea.objects.create(
        unit=unit,
        name="Ensuite",
        area_type=PropertyArea.AreaType.BATHROOM,
        kind=PropertyArea.Kind.PRIVATE,
    )
    ensuite.serves_areas.set([master])

    assert unit.areas.count() == 2
    assert list(ensuite.serves_areas.all()) == [master]
    # No Property row was needed to express any of this.
    assert Property.objects.filter(unit=unit).count() == 0


def test_area_must_have_a_parent(landlord):
    orphan = PropertyArea(name="Nowhere", area_type=PropertyArea.AreaType.KITCHEN)
    with pytest.raises(ValidationError):
        orphan.clean()


def test_exclusive_area_requires_a_room(landlord, unit):
    area = PropertyArea(
        unit=unit,
        name="Ensuite",
        area_type=PropertyArea.AreaType.BATHROOM,
        kind=PropertyArea.Kind.EXCLUSIVE,
    )
    with pytest.raises(ValidationError):
        area.clean()


def test_seeded_defaults_are_flagged_and_do_not_count_as_recorded_layout(unit):
    """Scaffolding must never read as fact.

    seed_default_areas invents a plausible set (Kitchen, Laundry, Garage...)
    so maintenance and inspections have something to reference. If those were
    indistinguishable from recorded layout, "we know nothing about this floor"
    would silently become "it has a garage", which is exactly the kind of
    invention this whole change exists to prevent.
    """
    created = seed_default_areas(unit=unit)

    assert created, "expected a starter set"
    assert all(area.is_seeded_default for area in created)
    assert unit.areas.filter(is_seeded_default=False).count() == 0

    PropertyArea.objects.create(
        unit=unit,
        name="Master Bedroom",
        area_type=PropertyArea.AreaType.BEDROOM,
        kind=PropertyArea.Kind.PRIVATE,
    )
    assert unit.areas.filter(is_seeded_default=False).count() == 1


def test_seeding_is_idempotent(unit):
    first = seed_default_areas(unit=unit)
    second = seed_default_areas(unit=unit)

    assert first and not second
    assert unit.areas.count() == len(first)


def test_label_falls_back_to_area_type(unit):
    named = PropertyArea.objects.create(
        unit=unit, name="Master Bedroom", area_type=PropertyArea.AreaType.BEDROOM
    )
    unnamed = PropertyArea.objects.create(
        unit=unit, area_type=PropertyArea.AreaType.KITCHEN
    )
    assert named.label == "Master Bedroom"
    assert unnamed.label == "Kitchen"


def test_tenant_sees_their_rooms_unit_and_group_areas(landlord, unit):
    """A tenant's territory spans whichever parent actually holds the areas."""
    group = PropertyGroup.objects.create(
        landlord=landlord, unit=unit, name="Main Floor Rooms"
    )
    room = Property.objects.create(
        landlord=landlord,
        name="Room 1",
        address="3213 Wascana St",
        city="Victoria",
        province="bc",
        property_category=Property.PropertyCategory.ROOM,
        room_type=Property.RoomType.PRIVATE,
        group=group,
        unit=unit,
    )
    group_kitchen = PropertyArea.objects.create(
        group=group, name="Kitchen", area_type=PropertyArea.AreaType.KITCHEN
    )
    unit_furnace = PropertyArea.objects.create(
        unit=unit,
        name="Furnace",
        area_type=PropertyArea.AreaType.HEATING,
        kind=PropertyArea.Kind.SYSTEM,
    )
    ensuite = PropertyArea.objects.create(
        group=group,
        name="Ensuite",
        area_type=PropertyArea.AreaType.BATHROOM,
        kind=PropertyArea.Kind.EXCLUSIVE,
        exclusive_to=room,
    )
    other_room = Property.objects.create(
        landlord=landlord,
        name="Room 2",
        address="3213 Wascana St",
        city="Victoria",
        province="bc",
        property_category=Property.PropertyCategory.ROOM,
        room_type=Property.RoomType.PRIVATE,
        group=group,
        unit=unit,
    )
    not_theirs = PropertyArea.objects.create(
        group=group,
        name="Room 2 Ensuite",
        area_type=PropertyArea.AreaType.BATHROOM,
        kind=PropertyArea.Kind.EXCLUSIVE,
        exclusive_to=other_room,
    )

    visible = set(areas_for_tenant_room(room))
    assert {group_kitchen, unit_furnace, ensuite} <= visible
    assert not_theirs not in visible
