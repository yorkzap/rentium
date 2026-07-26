"""RAMA describing a building in units.

The scenario that started this work: "McCaughey Main Floor is one complete unit
— inside it there are these rooms." The old create_house_layout turned every
described bedroom into a rentable ROOM listing and every floor name into a
PropertyGroup, so a floor let as one home came out as three separate room
listings. These tests pin the rule that replaced it:

    a bedroom described inside a floor is INTERNAL LAYOUT, not an offering.
"""

import json
from datetime import date

import pytest

from rentium.leases.models import Lease
from rentium.properties.models import Property
from rentium.properties.models import PropertyArea
from rentium.properties.models import PropertyUnit
from rentium.rama import registry

pytestmark = pytest.mark.django_db

MCCAUGHEY_MAIN_FLOOR = {
    "name": "Main Floor",
    "rental_mode": "WHOLE_UNIT",
    "spaces": [
        {"name": "Master Bedroom", "type": "BEDROOM"},
        {"name": "Bedroom 2", "type": "BEDROOM"},
        {"name": "Bedroom 3", "type": "BEDROOM"},
        {
            "name": "Master Ensuite",
            "type": "BATHROOM",
            "access": "PRIVATE",
            "serves": ["Master Bedroom"],
        },
        {
            "name": "Second Bathroom",
            "type": "BATHROOM",
            "serves": ["Bedroom 2", "Bedroom 3"],
        },
        {"name": "Kitchen"},
        {"name": "Living Room"},
    ],
}


def _structure(landlord, units, confirm="yes", **kw):
    return registry.execute(
        "create_property_structure",
        {
            "holding_name": "McCaughey House",
            "address": "5654 McCaughey Street",
            "city": "Regina",
            "province": "sk",
            "units_json": json.dumps(units),
            "confirm": confirm,
            **kw,
        },
        landlord=landlord,
    )


# ------------------------------------------------- the core interpretation
def test_a_three_bedroom_floor_let_whole_makes_one_listing(landlord):
    """The bug in one test: three bedrooms, ONE thing on the market."""
    result = _structure(landlord, [MCCAUGHEY_MAIN_FLOOR])
    assert result.get("created"), result

    unit = PropertyUnit.objects.get(name="Main Floor")
    assert unit.rental_mode == PropertyUnit.RentalMode.WHOLE_UNIT

    offerings = list(unit.active_offerings())
    assert len(offerings) == 1
    assert offerings[0].property_category == Property.PropertyCategory.COMPLETE_UNIT
    assert offerings[0].bedrooms == 3
    assert offerings[0].bathrooms == 2

    # Not one ROOM listing anywhere.
    assert not Property.objects.filter(
        unit=unit, property_category=Property.PropertyCategory.ROOM
    ).exists()


def test_the_bedrooms_are_recorded_as_layout(landlord):
    _structure(landlord, [MCCAUGHEY_MAIN_FLOOR])
    unit = PropertyUnit.objects.get(name="Main Floor")

    names = set(
        unit.areas.filter(is_seeded_default=False).values_list("name", flat=True)
    )
    assert names == {
        "Master Bedroom",
        "Bedroom 2",
        "Bedroom 3",
        "Master Ensuite",
        "Second Bathroom",
        "Kitchen",
        "Living Room",
    }
    assert unit.layout_complete is True


def test_which_bedrooms_each_bathroom_serves_is_kept(landlord):
    """"The master has its own; the other two share one" has to survive."""
    _structure(landlord, [MCCAUGHEY_MAIN_FLOOR])

    ensuite = PropertyArea.objects.get(name="Master Ensuite")
    shared = PropertyArea.objects.get(name="Second Bathroom")

    assert [a.name for a in ensuite.serves_areas.all()] == ["Master Bedroom"]
    assert sorted(a.name for a in shared.serves_areas.all()) == [
        "Bedroom 2",
        "Bedroom 3",
    ]


def test_by_room_makes_one_listing_per_bedroom(landlord):
    """The McKenzie case — genuinely let room by room — still works."""
    result = _structure(
        landlord,
        [
            {
                "name": "Upstairs",
                "rental_mode": "BY_ROOM",
                "spaces": [
                    {"name": "Room A", "type": "BEDROOM"},
                    {"name": "Room B", "type": "BEDROOM"},
                    {"name": "Kitchen"},
                    {"name": "Bathroom", "type": "BATHROOM"},
                ],
            }
        ],
    )
    assert result.get("created")

    unit = PropertyUnit.objects.get(name="Upstairs")
    offerings = {p.name for p in unit.active_offerings()}
    assert offerings == {"Room A", "Room B"}
    assert not unit.offerings.filter(
        property_category=Property.PropertyCategory.COMPLETE_UNIT,
        is_active_offering=True,
    ).exists()


# ----------------------------------------------------------- the question
def test_an_undeclared_rental_mode_asks_once_instead_of_guessing(landlord):
    """Guessing here is what produced three listings for one home — and the
    landlord could not see that it had guessed."""
    result = _structure(
        landlord,
        [{"name": "Main Floor", "spaces": [{"name": "Bedroom 1", "type": "BEDROOM"}]}],
    )

    assert result.get("needs_answer")
    assert "single lease" in result["question_for_user"]
    assert PropertyUnit.objects.count() == 0
    assert Property.objects.count() == 0


def test_the_question_is_asked_once_for_all_undecided_units(landlord):
    result = _structure(
        landlord,
        [{"name": "Main Floor"}, {"name": "Basement"}],
    )
    assert result.get("needs_answer")
    assert "Main Floor, Basement" in result["question_for_user"]


def test_plain_language_settles_the_mode_without_asking(landlord):
    result = _structure(
        landlord,
        [
            {
                "name": "Main Floor",
                "note": "we rent the whole floor together on one lease",
                "spaces": [
                    {"name": "Bedroom 1", "type": "BEDROOM"},
                    {"name": "Bathroom", "type": "BATHROOM"},
                ],
            }
        ],
    )
    assert result.get("created")
    unit = PropertyUnit.objects.get(name="Main Floor")
    assert unit.rental_mode == PropertyUnit.RentalMode.WHOLE_UNIT


# ------------------------------------------------------------- incomplete
def test_missing_details_are_flagged_not_invented(landlord):
    result = _structure(
        landlord,
        [
            {
                "name": "Basement",
                "rental_mode": "WHOLE_UNIT",
                "spaces": [{"name": "Bedroom 1", "type": "BEDROOM"}],
                "missing": "Bathroom count unknown.",
            }
        ],
    )
    assert result.get("created")

    unit = PropertyUnit.objects.get(name="Basement")
    assert unit.layout_complete is False
    assert unit.missing_layout_notes == "Bathroom count unknown."
    # No bathroom was conjured to fill the gap.
    assert not unit.areas.filter(
        area_type=PropertyArea.AreaType.BATHROOM, is_seeded_default=False
    ).exists()


# ---------------------------------------------------------------- preview
def test_nothing_is_written_before_confirmation(landlord):
    result = _structure(landlord, [MCCAUGHEY_MAIN_FLOOR], confirm="")

    assert result.get("needs_confirm")
    assert result["preview"]["units"][0]["bedrooms"] == 3
    assert PropertyUnit.objects.count() == 0
    assert Property.objects.count() == 0


def test_running_the_same_structure_twice_does_not_duplicate(landlord):
    _structure(landlord, [MCCAUGHEY_MAIN_FLOOR])
    _structure(landlord, [MCCAUGHEY_MAIN_FLOOR])

    assert PropertyUnit.objects.filter(name="Main Floor").count() == 1
    assert (
        Property.objects.filter(
            property_category=Property.PropertyCategory.COMPLETE_UNIT
        ).count()
        == 1
    )


# ------------------------------------------------------------ layout only
def test_recording_layout_never_creates_a_listing(landlord):
    _structure(landlord, [MCCAUGHEY_MAIN_FLOOR])
    before = Property.objects.count()

    result = registry.execute(
        "update_unit_layout",
        {
            "unit_name": "Main Floor",
            "spaces_json": json.dumps(
                [{"name": "Bedroom 4", "type": "BEDROOM"}]
            ),
            "confirm": "yes",
        },
        landlord=landlord,
    )
    assert result.get("updated")
    assert Property.objects.count() == before


# -------------------------------------------------------------- mode switch
def test_mode_switch_is_refused_while_a_lease_is_live(landlord):
    _structure(landlord, [MCCAUGHEY_MAIN_FLOOR])
    unit = PropertyUnit.objects.get(name="Main Floor")
    listing = unit.active_offerings().first()
    Lease.objects.create(
        landlord=landlord,
        property=listing,
        lease_type=Lease.LeaseType.BC_RESIDENTIAL_TENANCY,
        status=Lease.LeaseStatus.ACTIVE,
        start_date=date.today(),
        is_month_to_month=True,
        total_rent="2400.00",
    )

    result = registry.execute(
        "set_unit_rental_mode",
        {"unit_name": "Main Floor", "rental_mode": "BY_ROOM", "confirm": "yes"},
        landlord=landlord,
    )
    assert "error" in result
    assert result["blocked_by"]
    unit.refresh_from_db()
    assert unit.rental_mode == PropertyUnit.RentalMode.WHOLE_UNIT


def test_mode_switch_parks_rather_than_deletes(landlord):
    _structure(landlord, [MCCAUGHEY_MAIN_FLOOR])
    unit = PropertyUnit.objects.get(name="Main Floor")
    whole = unit.active_offerings().first()

    result = registry.execute(
        "set_unit_rental_mode",
        {"unit_name": "Main Floor", "rental_mode": "BY_ROOM", "confirm": "yes"},
        landlord=landlord,
    )
    assert result.get("ok")

    whole.refresh_from_db()
    assert whole.is_active_offering is False
    assert Property.objects.filter(pk=whole.pk).exists()
