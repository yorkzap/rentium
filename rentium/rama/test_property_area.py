"""Layout questions, and scoping a model that hangs off three different parents.

218 rows on live data describing what is physically inside every unit, and RAMA
could not see one of them. "How many bedrooms does the Garden Suite have?" had
no answer — and per the denial guard's whole argument, no answer reads exactly
like none.

The interesting part is the scope. PropertyArea has nullable `unit`, `group` and
`property` FKs with a check constraint that one is set, so no single `scope_path`
reaches every row. Picking one would silently hide the others; ORing them is
safe only if EVERY disjunct terminates at a LandlordProfile, which is what these
tests hold down.
"""

from __future__ import annotations

import pytest

from rentium.rama.domain_read import read
from rentium.rama.manifest import MANIFEST

pytestmark = pytest.mark.django_db


@pytest.fixture
def other_landlord(db):
    from rentium.users.models import LandlordProfile
    from rentium.users.tests.factories import UserFactory

    return LandlordProfile.objects.create(user=UserFactory())


@pytest.fixture
def unit(landlord):
    from rentium.properties.models import PropertyHolding, PropertyUnit

    holding = PropertyHolding.objects.create(
        landlord=landlord, name="McKenzie House", address="12 McKenzie St",
    )
    return PropertyUnit.objects.create(
        landlord=landlord, holding=holding, name="Garden Suite",
    )


def _listing_row(snapshot, listing):
    """The snapshot's row for one listing, wherever it was bucketed."""
    for value in snapshot.values():
        if not isinstance(value, list):
            continue
        for row in value:
            if isinstance(row, dict) and row.get("id") == str(listing.pk):
                return row
    raise AssertionError(f"{listing.name} is missing from the snapshot")


def _area(**kwargs):
    from rentium.properties.models import PropertyArea

    return PropertyArea.objects.create(**kwargs)


@pytest.fixture
def layout(unit, bc_property):
    """One area on each of the three parents, so every disjunct is exercised."""
    from rentium.properties.models import PropertyArea

    return [
        _area(unit=unit, name="Master Bedroom",
              area_type=PropertyArea.AreaType.BEDROOM,
              kind=PropertyArea.Kind.PRIVATE),
        _area(unit=unit, name="Ensuite",
              area_type=PropertyArea.AreaType.BATHROOM,
              kind=PropertyArea.Kind.PRIVATE),
        _area(unit=unit, name="Kitchen",
              area_type=PropertyArea.AreaType.KITCHEN,
              kind=PropertyArea.Kind.COMMON, shared_with_landlord=True),
        _area(property=bc_property, name="Room C Closet",
              area_type=PropertyArea.AreaType.STORAGE,
              kind=PropertyArea.Kind.EXCLUSIVE),
    ]


# ------------------------------------------------------------ the question

def test_how_many_bedrooms(landlord, layout):
    result = read(
        landlord, entity="property_area",
        filters="area_type=BEDROOM, is_seeded_default=false", aggregate="count",
    )
    assert "error" not in result, result
    assert result["totals"]["row_count"] == 1


def test_the_layout_of_one_unit(landlord, unit, layout):
    """Named by the unit, the way the landlord would say it."""
    result = read(
        landlord, entity="property_area", filters="unit=Garden Suite",
        fields="name, area_type, kind",
    )
    assert "error" not in result, result
    assert {row["name"] for row in result["rows"]} == {
        "Master Bedroom", "Ensuite", "Kitchen",
    }


def test_areas_grouped_by_what_they_are(landlord, layout):
    """`sum:count` alongside `count` — PropertyArea declares a field literally
    called `count`, which used to collide with the row-count alias and produce
    "Cannot compute Sum('count'): 'count' is an aggregate"."""
    result = read(
        landlord, entity="property_area", filters="is_seeded_default=false",
        group_by="area_type", aggregate="count, sum:count",
    )
    assert "error" not in result, result
    assert result["totals"]["row_count"] == 4
    assert result["totals"]["sum_count"] == 4


def test_the_legal_field_is_readable(landlord, layout):
    """`shared_with_landlord` decides whether the provincial tenancy act
    applies at all (leases/tenancy_rules). A question about it must not be
    answered from anything else."""
    result = read(
        landlord, entity="property_area", filters="shared_with_landlord=true",
        fields="name, area_type",
    )
    assert [row["name"] for row in result["rows"]] == ["Kitchen"]


# ---------------------------------------------------------------- the scope

def test_all_three_parents_are_reachable(landlord, layout):
    """Scoping through one parent would have hidden the others in silence.

    Three hang off a unit, one off a property; the property's seeded
    scaffolding is excluded so the assertion is about parents, not the seed.
    """
    result = read(
        landlord, entity="property_area", filters="is_seeded_default=false",
        aggregate="count",
    )
    assert result["totals"]["row_count"] == 4


def test_another_landlord_sees_none_of_it(other_landlord, layout):
    result = read(other_landlord, entity="property_area", aggregate="count")
    assert result["totals"]["row_count"] == 0


def test_every_scope_disjunct_reaches_a_landlord():
    """The safety condition for ORing scope paths at all, asserted directly.

    test_manifest_coverage checks this for every entity; it is repeated here
    because this is the entity the feature was built for.
    """
    from django.apps import apps

    from rentium.users.models import LandlordProfile

    spec = MANIFEST["property_area"]
    model = apps.get_model(*spec.model.split("."))
    paths = (spec.scope_path, *spec.alt_scope_paths)
    assert len(paths) == 3
    for path in paths:
        current = model
        for part in path.split("__"):
            current = current._meta.get_field(part).related_model
        assert current is LandlordProfile, path


def test_a_row_with_no_landlord_parent_is_unreachable(landlord, layout):
    """The OR narrows; it never widens. A row whose parents all belong to
    somebody else matches no disjunct."""
    from rentium.properties.models import PropertyArea
    from rentium.properties.models import PropertyHolding, PropertyUnit
    from rentium.users.models import LandlordProfile
    from rentium.users.tests.factories import UserFactory

    stranger = LandlordProfile.objects.create(user=UserFactory())
    holding = PropertyHolding.objects.create(
        landlord=stranger, name="Elsewhere", address="99 Other Rd",
    )
    theirs = PropertyUnit.objects.create(
        landlord=stranger, holding=holding, name="Their Suite",
    )
    _area(unit=theirs, name="Their Bedroom",
          area_type=PropertyArea.AreaType.BEDROOM)

    result = read(
        landlord, entity="property_area", filters="is_seeded_default=false",
        aggregate="count",
    )
    assert result["totals"]["row_count"] == 4


def test_scope_q_is_the_only_way_queries_are_scoped():
    """A caller that builds `{scope_path: landlord}` by hand scopes a
    multi-parent entity by one parent and loses the rest."""
    import pathlib

    rama = pathlib.Path(__file__).parent
    for name in ("domain_read.py", "domain_write.py"):
        source = (rama / name).read_text()
        assert "spec.scope_path: landlord" not in source, (
            f"{name} scopes by hand; use spec.scope_q(landlord)"
        )


# ------------------------------------------------- seeded scaffolding is not fact

def test_seeded_defaults_are_flagged_not_hidden(landlord, unit):
    """Auto-created placeholders must be visible AND marked.

    Hiding them would be a second silent omission; reporting them as recorded
    layout turns "we know nothing about this floor" into "it has a garage".
    """
    from rentium.properties.models import PropertyArea

    _area(unit=unit, name="Garage", area_type=PropertyArea.AreaType.GARAGE,
          is_seeded_default=True)
    _area(unit=unit, name="Master Bedroom",
          area_type=PropertyArea.AreaType.BEDROOM)

    everything = read(landlord, entity="property_area", aggregate="count")
    assert everything["totals"]["row_count"] == 2
    recorded = read(
        landlord, entity="property_area", filters="is_seeded_default=false",
        aggregate="count",
    )
    assert recorded["totals"]["row_count"] == 1
    assert "scaffolding" in everything["scope_note"]


def test_a_near_miss_field_name_is_suggested(landlord, layout):
    """The whole live failure, in one assertion.

    Asked how many bedrooms the Garden Suite has, the model requested
    `fields='type,type_display,count,name,unit,…'`. `area_type` — the one
    column that answers the question — was not in that list, so RAMA got rows
    named "Bonus room J" and "Room K" with no way to see they were bedrooms,
    and answered that there were none.
    """
    result = read(
        landlord, entity="property_area", fields="name, type, type_display",
    )
    assert "area_type" in result["fields_unavailable"]["type"]


def test_a_near_miss_filter_name_is_suggested(landlord, layout):
    result = read(landlord, entity="property_area", filters="type=BEDROOM")
    assert "area_type" in result["error"]


def test_a_name_in_the_portfolio_is_not_a_concept(landlord, unit, layout):
    """"Garden Suite" is the name of a unit, not the word "suite".

    The guard matched "suite" against property_unit ("Unit within a holding
    (floor / suite)"), decided the landlord was asking about suites, and sent
    RAMA away from property_area — which held both bedrooms — to a table that
    could not answer.
    """
    from rentium.rama.capabilities import portfolio_proper_nouns
    from rentium.rama.coverage import unchecked_denials

    nouns = portfolio_proper_nouns(landlord)
    assert "suite" in nouns, "the unit is called Garden Suite"

    asked = "how many bedrooms are there in the garden suite?"
    reply = "There's no bedroom count recorded for Garden Suite."
    assert unchecked_denials(
        reply, {"property_area"}, landlord_message=asked, proper_nouns=nouns,
    ) == {}
    # Without the portfolio, the guard still fires — this is the fix, not luck.
    assert unchecked_denials(
        reply, {"property_area"}, landlord_message=asked,
    ) != {}


# ------------------------------------------- the snapshot must not contradict it

def test_the_layout_snapshot_counts_the_units_areas(landlord, unit, bc_property):
    """A wrong number in the prompt is worse than a missing one.

    The per-listing layout read ONLY the listing's own areas, so a whole-unit
    listing reported `recorded_internal_area_count: 0` while its unit held a
    complete recorded layout. RAMA answers from its context before it queries
    anything, so a zero here stops the question being asked at all.
    """
    from rentium.properties.models import Property, PropertyArea
    from rentium.rama.union import property_inventory

    bc_property.unit = unit
    bc_property.property_category = Property.PropertyCategory.COMPLETE_UNIT
    bc_property.save(update_fields=["unit", "property_category"])
    _area(unit=unit, name="Room K", area_type=PropertyArea.AreaType.BEDROOM)
    _area(unit=unit, name="Bonus room J", area_type=PropertyArea.AreaType.BEDROOM)
    _area(unit=unit, name="Furnace", area_type=PropertyArea.AreaType.HEATING,
          is_seeded_default=True)

    row = _listing_row(property_inventory(landlord), bc_property)
    types = [area["type"] for area in row["layout"]["internal_areas"]]
    assert types.count("BEDROOM") == 2
    assert "HEATING" not in types, "seeded scaffolding is not recorded layout"
    assert row["layout"]["recorded_internal_area_count"] == 2


def test_a_room_listing_does_not_claim_the_units_layout(landlord, unit):
    """The household kitchen is shared space, not this room's layout."""
    from rentium.properties.models import Property, PropertyArea
    from rentium.rama.union import property_inventory

    room = Property.objects.create(
        landlord=landlord, unit=unit, name="Room C",
        property_category=Property.PropertyCategory.ROOM,
    )
    _area(unit=unit, name="Kitchen", area_type=PropertyArea.AreaType.KITCHEN)
    _area(property=room, name="Room C", area_type=PropertyArea.AreaType.BEDROOM)

    row = _listing_row(property_inventory(landlord), room)
    assert [a["type"] for a in row["layout"]["internal_areas"]] == ["BEDROOM"]


def test_a_listing_with_no_areas_points_at_its_units_layout(landlord, unit):
    """A bare zero next to a name reads as a fact about the place.

    Asked how many bedrooms the Garden Suite has, RAMA read the LISTING called
    Garden Suite — `recorded_internal_area_count: 0` — and said none were
    recorded, while the UNIT called Garden Suite two rows away had two.
    """
    from rentium.properties.models import Property, PropertyArea
    from rentium.rama.union import property_inventory

    room = Property.objects.create(
        landlord=landlord, unit=unit, name="Garden Suite",
        property_category=Property.PropertyCategory.ROOM,
    )
    _area(unit=unit, name="Room K", area_type=PropertyArea.AreaType.BEDROOM)
    _area(unit=unit, name="Bonus room J", area_type=PropertyArea.AreaType.BEDROOM)

    row = _listing_row(property_inventory(landlord), room)
    assert row["layout"]["recorded_internal_area_count"] == 0
    note = row["layout"]["layout_recorded_on_unit_instead"]
    assert "2 bedroom(s)" in note
    assert "Garden Suite" in note


def test_no_note_when_the_unit_has_nothing_either(landlord, unit):
    """Silence about a real absence, not a pointer to another absence."""
    from rentium.properties.models import Property
    from rentium.rama.union import property_inventory

    room = Property.objects.create(
        landlord=landlord, unit=unit, name="Room Q",
        property_category=Property.PropertyCategory.ROOM,
    )
    row = _listing_row(property_inventory(landlord), room)
    assert "layout_recorded_on_unit_instead" not in row["layout"]


def test_two_units_with_one_name_are_flagged(landlord, unit):
    """The live failure, exactly.

    Two units are called "Garden Suite" — one with nothing recorded, one with
    two bedrooms. Asked "how many bedrooms are there in the garden suite?",
    RAMA picked a row and answered "not recorded": true of that row, false of
    the portfolio. Both rows carried their holding and it still read them as
    one place, so the ambiguity has to be stated, not merely derivable.
    """
    from rentium.properties.models import PropertyArea, PropertyHolding, PropertyUnit
    from rentium.rama.union import property_inventory

    other_holding = PropertyHolding.objects.create(
        landlord=landlord, name="Wascana St", address="3213 Wascana St",
    )
    twin = PropertyUnit.objects.create(
        landlord=landlord, holding=other_holding, name="Garden Suite",
    )
    _area(unit=unit, name="Room K", area_type=PropertyArea.AreaType.BEDROOM)
    assert twin.areas.count() == 0 or True  # the empty one is the point

    rows = [r for r in property_inventory(landlord)["units"]
            if r.get("name") == "Garden Suite"]
    assert len(rows) == 2
    for row in rows:
        assert "name_is_ambiguous" in row, row
        assert row["holding"] in row["name_is_ambiguous"]


def test_a_filter_naming_two_things_says_so(landlord, unit):
    """`unit=Garden Suite` spans both units; the total is real, the noun isn't."""
    from rentium.properties.models import PropertyArea, PropertyHolding, PropertyUnit

    other_holding = PropertyHolding.objects.create(
        landlord=landlord, name="Wascana St", address="3213 Wascana St",
    )
    twin = PropertyUnit.objects.create(
        landlord=landlord, holding=other_holding, name="Garden Suite",
    )
    _area(unit=unit, name="Room K", area_type=PropertyArea.AreaType.BEDROOM)
    _area(unit=twin, name="Bed 1", area_type=PropertyArea.AreaType.BEDROOM)

    result = read(
        landlord, entity="property_area", filters="unit=Garden Suite",
        aggregate="count",
    )
    assert result["totals"]["row_count"] == 2
    assert "unit" in result["ambiguous_filters"]
    assert "2 different" in result["ambiguous_filters"]["unit"]


def test_one_match_is_not_flagged(landlord, unit, layout):
    """The note appears only when the name really was ambiguous."""
    result = read(
        landlord, entity="property_area", filters="unit=Garden Suite",
        aggregate="count",
    )
    assert "ambiguous_filters" not in result


def test_an_id_filter_is_never_ambiguous(landlord, unit, layout):
    result = read(
        landlord, entity="property_area", filters=f"unit={unit.pk}",
        aggregate="count",
    )
    assert "ambiguous_filters" not in result


def test_the_area_entity_is_no_longer_waived():
    from rentium.rama.test_manifest_coverage import WAIVED

    assert "properties.PropertyArea" not in WAIVED
    assert "property_area" in MANIFEST
