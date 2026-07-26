"""The portfolio restructure command: non-destructive and idempotent.

The command carries a declarative spec for one real portfolio, so these tests
build the McCaughey slice of it and assert the properties that must hold no
matter what the spec says: nothing is deleted, room listings are parked rather
than removed, recorded layout is never left flagged as scaffolding, and a
second run changes nothing.
"""

from io import StringIO

import pytest
from django.core.management import call_command

from rentium.properties.models import Property
from rentium.properties.models import PropertyArea
from rentium.properties.models import PropertyGroup
from rentium.properties.models import PropertyHolding
from rentium.properties.models import PropertyUnit

pytestmark = pytest.mark.django_db


@pytest.fixture
def mccaughey(landlord):
    """The three bedrooms of McCaughey Main Floor, as they were stored: three
    individually-rentable room listings in a group."""
    holding = PropertyHolding.objects.create(
        landlord=landlord,
        name="5654 McCaughey Street",
        address="5654 McCaughey Street",
        city="Regina",
    )
    group = PropertyGroup.objects.create(
        landlord=landlord, name="McCaughey Main Floor"
    )
    rooms = [
        Property.objects.create(
            landlord=landlord,
            holding=holding,
            group=group,
            name=name,
            address="5654 McCaughey Street",
            city="Regina",
            province="sk",
            property_category=Property.PropertyCategory.ROOM,
            room_type=Property.RoomType.PRIVATE,
        )
        for name in (
            "McCaughey Master Bedroom",
            "McCaughey Main Floor Room 2",
            "McCaughey Main Floor Room 3",
        )
    ]
    return {"holding": holding, "group": group, "rooms": rooms}


def _run(**kwargs):
    out = StringIO()
    call_command("migrate_to_units", stdout=out, **kwargs)
    return out.getvalue()


def test_dry_run_writes_nothing(mccaughey):
    _run(dry_run=True)

    assert PropertyUnit.objects.count() == 0
    assert Property.objects.filter(is_active_offering=False).count() == 0


def test_a_floor_of_rooms_becomes_one_whole_unit(mccaughey):
    _run()

    unit = PropertyUnit.objects.get(
        holding=mccaughey["holding"], name="Main Floor"
    )
    assert unit.rental_mode == PropertyUnit.RentalMode.WHOLE_UNIT
    assert unit.layout_complete is True

    offerings = list(unit.active_offerings())
    assert len(offerings) == 1
    assert offerings[0].name == "McCaughey Main Floor"
    assert offerings[0].property_category == Property.PropertyCategory.COMPLETE_UNIT
    assert offerings[0].bedrooms == 3


def test_room_listings_are_parked_not_deleted(mccaughey):
    original_pks = {room.pk for room in mccaughey["rooms"]}

    _run()

    survivors = Property.objects.filter(pk__in=original_pks)
    assert survivors.count() == 3, "room listings must survive"
    assert not survivors.filter(is_active_offering=True).exists()
    # Still attached to their unit, so switching back to BY_ROOM finds them.
    assert survivors.filter(unit__isnull=True).count() == 0


def test_the_group_is_kept_and_linked_to_its_unit(mccaughey):
    _run()

    group = mccaughey["group"]
    group.refresh_from_db()
    assert group.unit is not None
    assert group.unit.name == "Main Floor"


def test_recorded_layout_is_not_left_flagged_as_scaffolding(mccaughey):
    """Creating the unit's listing fires the area-seeding signal first, so a
    generic "Kitchen" placeholder exists before the real layout is written.
    The real spaces must be promoted out of scaffolding, or RAMA goes on
    reporting a floor we DO know about as unknown."""
    _run()

    unit = PropertyUnit.objects.get(
        holding=mccaughey["holding"], name="Main Floor"
    )
    recorded = unit.areas.filter(is_seeded_default=False)
    names = set(recorded.values_list("name", flat=True))

    assert names == {
        "Master Bedroom",
        "Bedroom 2",
        "Bedroom 3",
        "Master Ensuite",
        "Second Bathroom",
        "Kitchen",
        "Living Room",
    }


def test_bathrooms_record_which_bedrooms_they_serve(mccaughey):
    _run()

    ensuite = PropertyArea.objects.get(name="Master Ensuite")
    shared = PropertyArea.objects.get(name="Second Bathroom")

    assert [a.name for a in ensuite.serves_areas.all()] == ["Master Bedroom"]
    assert sorted(a.name for a in shared.serves_areas.all()) == [
        "Bedroom 2",
        "Bedroom 3",
    ]


def test_incomplete_layout_is_flagged_rather_than_invented(mccaughey):
    """An empty basement gets a usable unit that says what it doesn't know."""
    _run()

    basement = PropertyUnit.objects.get(
        holding=mccaughey["holding"], name="Basement"
    )
    assert basement.layout_complete is False
    assert basement.missing_layout_notes
    assert basement.areas.filter(is_seeded_default=False).count() == 0


def test_running_twice_changes_nothing(mccaughey):
    _run()
    snapshot = {
        "units": PropertyUnit.objects.count(),
        "listings": Property.objects.count(),
        "active": Property.objects.filter(is_active_offering=True).count(),
        "areas": PropertyArea.objects.count(),
        "recorded": PropertyArea.objects.filter(is_seeded_default=False).count(),
    }

    _run()

    assert {
        "units": PropertyUnit.objects.count(),
        "listings": Property.objects.count(),
        "active": Property.objects.filter(is_active_offering=True).count(),
        "areas": PropertyArea.objects.count(),
        "recorded": PropertyArea.objects.filter(is_seeded_default=False).count(),
    } == snapshot
