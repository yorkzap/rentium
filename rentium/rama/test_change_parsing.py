"""The `update` tool's change parser.

Reconstructed from the audit log: `update` had a 76% failure rate (53 errors in
70 calls), and 32 of those errors were the SAME correct call being rejected —

    changes='description=1 bedroom, living room, kitchen, washroom, private patio'

The parser split on every comma before looking for '=', so it read
'description=1 bedroom', then rejected 'living room' as a malformed clause. Any
free-text value containing a comma failed, which is most of them. The model was
doing nothing wrong.
"""

import pytest

from rentium.rama.domain_write import _parse_change_clauses
from rentium.rama.domain_write import _split_change_clauses

FIELDS = {"name", "description", "address", "city", "province", "postal_code", "status"}


def test_a_value_may_contain_commas():
    """The exact call from the log, which failed 32 times."""
    parsed, err = _parse_change_clauses(
        "description=1 bedroom, living room, kitchen, washroom, private patio",
        FIELDS,
    )
    assert err is None
    assert parsed == {
        "description": "1 bedroom, living room, kitchen, washroom, private patio"
    }


def test_several_changes_still_split():
    parsed, err = _parse_change_clauses(
        "name=Garden Suite, city=Victoria, province=bc", FIELDS
    )
    assert err is None
    assert parsed == {
        "name": "Garden Suite",
        "city": "Victoria",
        "province": "bc",
    }


def test_a_comma_value_followed_by_another_field():
    parsed, err = _parse_change_clauses(
        "description=1 bedroom, kitchen, private patio, city=Victoria", FIELDS
    )
    assert err is None
    assert parsed == {
        "description": "1 bedroom, kitchen, private patio",
        "city": "Victoria",
    }


def test_longest_field_name_wins_at_a_boundary():
    """`postal_code` must not be cut short by a prefix match."""
    parsed, err = _parse_change_clauses(
        "description=near the park, postal_code=V9A 1B5", FIELDS
    )
    assert err is None
    assert parsed["postal_code"] == "V9A 1B5"


def test_a_word_that_merely_looks_like_a_field_is_not_a_boundary():
    """'status' inside prose isn't a new clause unless it is followed by '='."""
    parsed, err = _parse_change_clauses(
        "description=good status, quiet street", FIELDS
    )
    assert err is None
    assert parsed == {"description": "good status, quiet street"}


def test_genuinely_unparseable_input_says_what_to_do():
    """A rejection has to be actionable — the old one just repeated the rule."""
    parsed, err = _parse_change_clauses("just some prose", FIELDS)
    assert parsed == {}
    assert err is not None
    assert "editable_fields" in err
    assert "example" in err
    assert "description" in err["editable_fields"]


@pytest.mark.parametrize("blank", ["", "   ", None])
def test_blank_input_is_no_changes_not_an_error(blank):
    parsed, err = _parse_change_clauses(blank, FIELDS)
    assert parsed == {}
    assert err is None


def test_split_falls_back_to_commas_when_no_fields_are_known():
    assert _split_change_clauses("a=1, b=2", set()) == ["a=1", "b=2"]


# --------------------------------------------------- idempotency & aliases
# Each case below is a real failure class from the audit log.

@pytest.mark.django_db
def test_recreating_a_group_reuses_it_instead_of_erroring(landlord):
    """12 of 20 create_property_group failures were "You already have a group
    named X" where the model had created that group a step earlier and was
    re-stating it. A create whose end state already holds has succeeded."""
    from rentium.rama import registry
    from rentium.properties.models import PropertyGroup

    first = registry.execute(
        "create_property_group", {"name": "Wascana Main Floor", "confirm": "yes"},
        landlord=landlord,
    )
    assert first.get("created")

    again = registry.execute(
        "create_property_group", {"name": "Wascana Main Floor", "confirm": "yes"},
        landlord=landlord,
    )
    assert again.get("reused") and not again.get("error")
    assert again["group"]["id"] == first["group"]["id"]
    assert PropertyGroup.objects.filter(landlord=landlord).count() == 1


@pytest.mark.django_db
def test_group_reuse_is_case_insensitive(landlord):
    """The log has both 'Wascana Main Floor' and 'Wascana Main floor'."""
    from rentium.rama import registry
    from rentium.properties.models import PropertyGroup

    registry.execute(
        "create_property_group", {"name": "Wascana Main Floor", "confirm": "yes"},
        landlord=landlord,
    )
    again = registry.execute(
        "create_property_group", {"name": "Wascana Main floor", "confirm": "yes"},
        landlord=landlord,
    )
    assert again.get("reused")
    assert PropertyGroup.objects.filter(landlord=landlord).count() == 1


@pytest.mark.django_db
def test_name_contains_filter_is_understood(landlord):
    """`read` rejected name_contains — the obvious name for a substring match."""
    from rentium.rama import registry
    from rentium.properties.models import Property

    Property.objects.create(
        landlord=landlord, name="Garden Suite", address="950 McKenzie Ave",
        city="Victoria", province="bc",
        property_category=Property.PropertyCategory.COMPLETE_UNIT,
        unit_type=Property.UnitType.GARDEN_SUITE,
    )
    Property.objects.create(
        landlord=landlord, name="Room A", address="950 McKenzie Ave",
        city="Victoria", province="bc",
        property_category=Property.PropertyCategory.ROOM,
        room_type=Property.RoomType.PRIVATE,
    )

    out = registry.execute(
        "read", {"entity": "property", "filters": "name_contains=Garden"},
        landlord=landlord,
    )
    assert "error" not in out
    assert [r["name"] for r in out["rows"]] == ["Garden Suite"]


@pytest.mark.django_db
def test_a_lease_can_be_filtered_by_its_property_name(landlord):
    """Asking for a lease by the room it is on is reasonable; it used to be
    "Can't filter on 'property_name'"."""
    from datetime import date

    from rentium.rama import registry
    from rentium.leases.models import Lease
    from rentium.properties.models import Property

    room = Property.objects.create(
        landlord=landlord, name="EvalUpd Room", address="950 McKenzie Ave",
        city="Victoria", province="bc",
        property_category=Property.PropertyCategory.ROOM,
        room_type=Property.RoomType.PRIVATE,
    )
    lease = Lease.objects.create(
        landlord=landlord, property=room,
        lease_type=Lease.LeaseType.GENERIC_ROOMMATE,
        status=Lease.LeaseStatus.ACTIVE, start_date=date.today(),
        is_month_to_month=True, total_rent="900.00",
    )

    out = registry.execute(
        "read", {"entity": "lease", "filters": "property_name~EvalUpd Room"},
        landlord=landlord,
    )
    assert "error" not in out
    assert [r["lease_number"] for r in out["rows"]] == [lease.lease_number]


@pytest.mark.django_db
def test_an_unknown_filter_is_still_rejected(landlord):
    """Aliases are a whitelist, not a general ORM escape hatch."""
    from rentium.rama import registry

    out = registry.execute(
        "read", {"entity": "property", "filters": "landlord__user__email=x@y.z"},
        landlord=landlord,
    )
    assert "Can't filter on" in out.get("error", "")
