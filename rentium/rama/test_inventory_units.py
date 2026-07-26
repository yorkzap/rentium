"""What RAMA says the landlord has.

Getting the domain model right fixed the dashboard but not RAMA's answer to
"what do I have?". Its inventory counted every Property row, so after the unit
migration it reported 21 listings and 14 rooms for a portfolio with 12 and 5 —
the parked listings, kept deliberately so a rental-mode switch can be undone,
were being counted as things on the market.
"""

import pytest

from rentium.properties.models import Property
from rentium.properties.models import PropertyArea
from rentium.properties.models import PropertyHolding
from rentium.properties.models import PropertyUnit
from rentium.rama import registry
from rentium.rama.union import property_inventory

pytestmark = pytest.mark.django_db


@pytest.fixture
def portfolio(landlord):
    """One floor let whole (with two bedrooms parked from a previous
    arrangement) and one floor genuinely let by the room."""
    holding = PropertyHolding.objects.create(
        landlord=landlord, name="5654 McCaughey Street",
        address="5654 McCaughey Street", city="Regina",
    )
    whole_unit = PropertyUnit.objects.create(
        landlord=landlord, holding=holding, name="Main Floor",
        rental_mode=PropertyUnit.RentalMode.WHOLE_UNIT, layout_complete=True,
    )
    by_room_unit = PropertyUnit.objects.create(
        landlord=landlord, holding=holding, name="Upstairs",
        rental_mode=PropertyUnit.RentalMode.BY_ROOM,
        missing_layout_notes="Shared bathroom not recorded.",
    )

    def listing(unit, name, category, active=True, **kw):
        return Property.objects.create(
            landlord=landlord, holding=holding, unit=unit, name=name,
            address="5654 McCaughey Street", city="Regina", province="sk",
            property_category=category, is_active_offering=active, **kw,
        )

    listing(
        whole_unit, "McCaughey Main Floor",
        Property.PropertyCategory.COMPLETE_UNIT,
        unit_type=Property.UnitType.MAIN_FLOOR,
    )
    for n in (1, 2):
        listing(
            whole_unit, f"Main Floor Room {n}", Property.PropertyCategory.ROOM,
            active=False, room_type=Property.RoomType.PRIVATE,
        )
    for n in (1, 2):
        listing(
            by_room_unit, f"Upstairs Room {n}", Property.PropertyCategory.ROOM,
            room_type=Property.RoomType.PRIVATE,
        )

    for name in ("Bedroom 1", "Bedroom 2"):
        PropertyArea.objects.create(
            unit=whole_unit, name=name, area_type=PropertyArea.AreaType.BEDROOM
        )
    PropertyArea.objects.create(
        unit=whole_unit, name="Bathroom", area_type=PropertyArea.AreaType.BATHROOM
    )
    PropertyArea.objects.create(
        unit=whole_unit, name="Laundry", area_type=PropertyArea.AreaType.LAUNDRY,
        is_seeded_default=True,
    )
    return {"whole": whole_unit, "by_room": by_room_unit}


def test_parked_listings_are_not_counted_as_inventory(landlord, portfolio):
    """3 live listings, 2 parked — not 5 listings."""
    counts = property_inventory(landlord)["counts"]

    assert counts["total_listings"] == 3
    assert counts["parked_listings"] == 2
    assert counts["rooms"] == 2, "only the genuinely-let-by-room bedrooms"
    assert counts["complete_units"] == 1


def test_the_physical_layer_is_reported(landlord, portfolio):
    """"How many places do I have?" is a question about units."""
    inv = property_inventory(landlord)

    assert inv["counts"]["physical_units"] == 2
    names = {u["name"] for u in inv["units"]}
    assert names == {"Main Floor", "Upstairs"}


def test_a_whole_unit_reports_its_bedrooms_without_listing_them(
    landlord, portfolio
):
    inv = property_inventory(landlord)
    main = next(u for u in inv["units"] if u["name"] == "Main Floor")

    assert main["bedrooms"] == 2
    assert main["bathrooms"] == 1
    assert main["listings"] == ["McCaughey Main Floor"]


def test_unrecorded_layout_reads_as_null_not_zero(landlord, portfolio):
    """A gap in what we know must never read as a fact about the building."""
    inv = property_inventory(landlord)
    upstairs = next(u for u in inv["units"] if u["name"] == "Upstairs")

    assert upstairs["bedrooms"] is None
    assert upstairs["bathrooms"] is None
    assert upstairs["not_recorded"] == "Shared bathroom not recorded."


def test_seeded_scaffolding_is_not_reported_as_layout(landlord, portfolio):
    """The auto-created "Laundry" is scaffolding, not something anyone said."""
    inv = property_inventory(landlord)
    main = next(u for u in inv["units"] if u["name"] == "Main Floor")

    # 2 bedrooms + 1 bathroom recorded; the seeded laundry is not among them.
    assert main["bedrooms"] == 2
    assert main["bathrooms"] == 1


def test_every_listing_says_which_unit_it_offers(landlord, portfolio):
    inv = property_inventory(landlord)
    rows = inv["rooms"] + inv["complete_units"]

    assert all(r["unit"] for r in rows)
    whole = next(r for r in rows if r["name"] == "McCaughey Main Floor")
    assert whole["rental_mode"] == PropertyUnit.RentalMode.WHOLE_UNIT


# ------------------------------------------------------------------ finders
def test_the_finder_does_not_return_parked_listings(landlord, portfolio):
    """This is what playbooks enumerate bulk-operation targets through — a
    bulk delete must not reach listings taken off the market by a mode switch."""
    out = registry.execute("find_listings", {}, landlord=landlord)
    names = {row["name"] for row in out["listings"]}

    assert "Main Floor Room 1" not in names
    assert names == {
        "McCaughey Main Floor",
        "Upstairs Room 1",
        "Upstairs Room 2",
    }


def test_parked_listings_are_still_reachable_on_request(landlord, portfolio):
    out = registry.execute(
        "find_listings", {"include_parked": "yes"}, landlord=landlord
    )
    names = {row["name"] for row in out["listings"]}
    assert "Main Floor Room 1" in names


def test_occupancy_ignores_parked_listings(landlord, portfolio):
    """Otherwise they report as vacant and inflate the vacancy picture."""
    out = registry.execute("occupancy_as_of", {}, landlord=landlord)
    blob = str(out)
    assert "Main Floor Room 1" not in blob
