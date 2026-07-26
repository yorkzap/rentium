"""New playbooks: retiring, visibility, and unit-scoped mode switching.

Only three playbooks existed, so most multi-step intents fell back to the model
emitting several previews in one turn and save_batch stitching them together —
which is why chaining worked sometimes and not others. These cover the gaps
that showed up in practice, including one the system was already ADVERTISING
without being able to perform: terminate_and_delete tells the landlord "retire
it — say: retire X", and until now nothing could.
"""

from datetime import date

import pytest

from rentium.leases.models import Lease
from rentium.properties.models import Property
from rentium.properties.models import PropertyHolding
from rentium.properties.models import PropertyUnit
from rentium.rama import registry

pytestmark = pytest.mark.django_db


@pytest.fixture
def portfolio(landlord):
    """Two houses; one of them has two floors, one let whole and one by room."""
    mck = PropertyHolding.objects.create(
        landlord=landlord, name="950 McKenzie Ave",
        address="950 McKenzie Ave", city="Victoria",
    )
    was = PropertyHolding.objects.create(
        landlord=landlord, name="3213 Wascana St",
        address="3213 Wascana St", city="Victoria",
    )
    main = PropertyUnit.objects.create(
        landlord=landlord, holding=was, name="Main Floor",
        rental_mode=PropertyUnit.RentalMode.WHOLE_UNIT,
    )
    basement = PropertyUnit.objects.create(
        landlord=landlord, holding=mck, name="Basement",
        rental_mode=PropertyUnit.RentalMode.BY_ROOM,
    )
    whole = Property.objects.create(
        landlord=landlord, holding=was, unit=main, name="Wascana Main Floor",
        address="3213 Wascana St", city="Victoria", province="bc",
        property_category=Property.PropertyCategory.COMPLETE_UNIT,
        unit_type=Property.UnitType.MAIN_FLOOR,
    )
    room = Property.objects.create(
        landlord=landlord, holding=mck, unit=basement, name="Room C",
        address="950 McKenzie Ave", city="Victoria", province="bc",
        property_category=Property.PropertyCategory.ROOM,
        room_type=Property.RoomType.PRIVATE,
    )
    return {"main": main, "basement": basement, "whole": whole, "room": room}


def _plan(landlord, **kw):
    return registry.execute("plan_operation", kw, landlord=landlord)


# ------------------------------------------------------------------ retire
def test_retire_is_now_an_action_not_just_advice(landlord, portfolio):
    out = _plan(landlord, operation="retire_listings", include="Room C")

    assert out.get("needs_confirm"), out
    steps = out["plan"]["steps"]
    assert len(steps) == 1
    assert steps[0]["tool"] == "update_property"
    assert steps[0]["arguments"]["status"] == "NOT_AVAILABLE"
    assert steps[0]["arguments"]["is_publicly_visible"] == "no"


def test_retiring_an_already_retired_listing_is_reported_not_repeated(
    landlord, portfolio
):
    portfolio["room"].status = Property.PropertyStatus.NOT_AVAILABLE
    portfolio["room"].is_publicly_visible = False
    portfolio["room"].save()

    out = _plan(landlord, operation="retire_listings", include="Room C")
    assert out["plan"]["steps"] == []
    assert out["plan"]["blocked"][0]["reason"] == "already"


# -------------------------------------------------------------- visibility
def test_set_visibility_needs_a_direction(landlord, portfolio):
    out = _plan(landlord, operation="set_visibility", include="Room C")
    assert out["plan"]["blocked"][0]["reason"] == "bad_visible"


def test_publishing_something_unpublishable_is_blocked_with_the_reason(
    landlord, portfolio
):
    """Queueing a step that changes nothing visible is worse than saying why."""
    out = _plan(
        landlord, operation="set_visibility", visible="yes", include="Room C"
    )
    blocked = out["plan"]["blocked"]
    if blocked:
        assert blocked[0]["reason"] in ("not_publishable", "already")
    else:
        assert out["plan"]["steps"][0]["arguments"]["is_publicly_visible"] == "yes"


def test_hiding_a_visible_listing_plans_one_step(landlord, portfolio):
    out = _plan(
        landlord, operation="set_visibility", visible="no", include="Room C"
    )
    assert out["plan"]["steps"][0]["arguments"]["is_publicly_visible"] == "no"


# ------------------------------------------------------- unit mode switching
def test_switching_mode_is_planned_per_unit(landlord, portfolio):
    out = _plan(
        landlord,
        operation="switch_rental_mode",
        new_mode="BY_ROOM",
        include="Main Floor",
    )
    steps = out["plan"]["steps"]
    assert len(steps) == 1
    assert steps[0]["tool"] == "set_unit_rental_mode"
    assert steps[0]["arguments"]["rental_mode"] == PropertyUnit.RentalMode.BY_ROOM
    # Reshaping the market pauses for its own yes inside a plan.
    assert steps[0]["requires_own_confirm"] is True


def test_a_unit_with_live_leases_is_blocked_with_the_lease_named(
    landlord, portfolio
):
    Lease.objects.create(
        landlord=landlord,
        property=portfolio["whole"],
        lease_type=Lease.LeaseType.BC_RESIDENTIAL_TENANCY,
        status=Lease.LeaseStatus.ACTIVE,
        start_date=date.today(),
        is_month_to_month=True,
        total_rent="2000.00",
    )

    out = _plan(
        landlord,
        operation="switch_rental_mode",
        new_mode="BY_ROOM",
        include="Main Floor",
    )
    assert out["plan"]["steps"] == []
    blocked = out["plan"]["blocked"][0]
    assert blocked["reason"] == "live_leases"
    assert blocked["leases"]


def test_switching_to_the_mode_a_unit_is_already_in_is_reported(
    landlord, portfolio
):
    out = _plan(
        landlord,
        operation="switch_rental_mode",
        new_mode="WHOLE_UNIT",
        include="Main Floor",
    )
    assert out["plan"]["steps"] == []
    assert out["plan"]["blocked"][0]["reason"] == "already"


def test_a_bad_mode_is_refused_before_anything_is_planned(landlord, portfolio):
    out = _plan(
        landlord, operation="switch_rental_mode", new_mode="sideways",
        include="Main Floor",
    )
    assert out["plan"]["blocked"][0]["reason"] == "bad_mode"


def test_an_ambiguous_unit_name_is_never_silently_resolved(landlord, portfolio):
    """Two houses can both have a "Basement". Restructuring the wrong floor is
    not something re-running fixes."""
    PropertyUnit.objects.create(
        landlord=landlord,
        holding=portfolio["main"].holding,
        name="Basement",
        rental_mode=PropertyUnit.RentalMode.WHOLE_UNIT,
    )

    out = _plan(
        landlord, operation="switch_rental_mode", new_mode="BY_ROOM",
        include="Basement",
    )
    assert out["plan"]["steps"] == []
    assert out["plan"]["blocked"][0]["reason"] == "ambiguous"


def test_holding_narrows_an_otherwise_ambiguous_unit_name(landlord, portfolio):
    PropertyUnit.objects.create(
        landlord=landlord,
        holding=portfolio["main"].holding,
        name="Basement",
        rental_mode=PropertyUnit.RentalMode.WHOLE_UNIT,
    )

    out = _plan(
        landlord,
        operation="switch_rental_mode",
        new_mode="WHOLE_UNIT",
        include="Basement",
        holding="950 McKenzie",
    )
    assert len(out["plan"]["steps"]) == 1


def test_no_units_matched_is_not_an_empty_plan(landlord, portfolio):
    out = _plan(
        landlord, operation="switch_rental_mode", new_mode="BY_ROOM",
        include="Attic",
    )
    assert out["plan"]["steps"] == []
    assert out["plan"]["blocked"][0]["reason"] == "unresolved"


# ------------------------------------------------------------- isolation
def test_units_of_other_landlords_are_invisible(landlord, portfolio):
    from rentium.users.models import LandlordProfile
    from rentium.users.tests.factories import UserFactory

    stranger = LandlordProfile.objects.create(user=UserFactory())
    out = _plan(
        stranger, operation="switch_rental_mode", new_mode="BY_ROOM",
        include="Main Floor",
    )
    assert out["plan"]["steps"] == []


# ------------------------------------------------------------ wrapper drift
# Registered tools in tools.py are thin wrappers around the real
# implementations. The registry builds each tool's JSON schema from the
# WRAPPER's signature and then drops any argument the model sends that isn't in
# it. So a parameter added to the implementation but not the wrapper is
# silently unreachable — the model passes it, it vanishes, and the tool behaves
# as if it were never sent.
#
# This bit twice. plan_move_tenant told the model "multiple listings match,
# pass pick", the model passed pick=oldest, and the registry discarded it
# because the wrapper had no such parameter — three identical failures with no
# way forward. Adding pick to the implementation alone did NOT fix it.

WRAPPED = {
    "plan_operation": "rentium.rama.playbooks:plan_operation",
    "plan_move_tenant": "rentium.rama.playbooks:plan_move_tenant",
    "create_property_structure": "rentium.rama.unit_structure:create_property_structure",
    "update_unit_layout": "rentium.rama.unit_structure:update_unit_layout",
    "set_unit_rental_mode": "rentium.rama.unit_structure:set_unit_rental_mode",
}


@pytest.mark.parametrize("tool_name,target", sorted(WRAPPED.items()))
def test_wrapper_exposes_every_argument_the_implementation_accepts(
    tool_name, target
):
    import importlib
    import inspect

    from rentium.rama.registry import REGISTRY

    module_path, func_name = target.split(":")
    impl = getattr(importlib.import_module(module_path), func_name)

    expected = {
        name
        for name, param in inspect.signature(impl).parameters.items()
        if name != "landlord"
        and param.kind
        not in (inspect.Parameter.VAR_POSITIONAL, inspect.Parameter.VAR_KEYWORD)
    }
    exposed = set(REGISTRY[tool_name].parameters["properties"])

    missing = expected - exposed
    assert not missing, (
        f"{tool_name}: the wrapper in tools.py does not expose {sorted(missing)}, "
        "so the registry will silently drop it."
    )
