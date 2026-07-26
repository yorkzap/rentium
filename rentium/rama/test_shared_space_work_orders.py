"""Reconstructed from a real conversation that went badly.

    landlord: McKenzie basement washroom's shower was leaking today.
    RAMA:     ...Please open the Maintenance dashboard to create a work order.
    landlord: create it
    RAMA:     Property: Room C — confirm?
    landlord: Room C, D, and F (McKenzie Basement)
    RAMA:     Property: Room C — confirm?
    landlord: not just c. its McKenzie Basement. the washroom is shared
    RAMA:     Property: Room C — confirm?

Three separate failures:

1. RAMA pointed at the dashboard for something it has a tool for.
2. create_work_order took only property_query, so "the shared washroom in
   McKenzie Basement" was unsayable — a work order could only belong to ONE
   listing.
3. Hence the loop: each correction produced the same preview, because there
   was nothing better for the model to say.

(2) is the root cause. A shared washroom belongs to the unit.
"""

import pytest

from rentium.maintenance.models import WorkOrder
from rentium.properties.models import Property
from rentium.properties.models import PropertyArea
from rentium.properties.models import PropertyHolding
from rentium.properties.models import PropertyUnit
from rentium.rama import registry
from rentium.rama.capabilities import supported_tool_for_request

pytestmark = pytest.mark.django_db


@pytest.fixture
def basement(landlord):
    """McKenzie Basement: three rooms sharing one washroom."""
    holding = PropertyHolding.objects.create(
        landlord=landlord, name="950 McKenzie Ave",
        address="950 McKenzie Ave", city="Victoria",
    )
    unit = PropertyUnit.objects.create(
        landlord=landlord, holding=holding, name="Basement",
        rental_mode=PropertyUnit.RentalMode.BY_ROOM,
    )
    rooms = [
        Property.objects.create(
            landlord=landlord, holding=holding, unit=unit, name=name,
            address="950 McKenzie Ave", city="Victoria", province="bc",
            property_category=Property.PropertyCategory.ROOM,
            room_type=Property.RoomType.PRIVATE,
        )
        for name in ("Room C", "Room D", "Room F")
    ]
    washroom = PropertyArea.objects.create(
        unit=unit, name="Shared Washroom",
        area_type=PropertyArea.AreaType.BATHROOM,
        kind=PropertyArea.Kind.COMMON,
    )
    return {"unit": unit, "rooms": rooms, "washroom": washroom}


def _create(landlord, **kw):
    return registry.execute(
        "create_work_order",
        {"title": "Shower leaking; hot water knob free", "priority": "HIGH",
         "category": "PLUMBING", **kw},
        landlord=landlord,
    )


# ------------------------------------------------------- the root failure
def test_a_shared_washroom_belongs_to_the_unit_not_one_room(landlord, basement):
    result = _create(
        landlord, property_query="McKenzie Basement", area="Shared Washroom",
        confirm="yes",
    )
    assert result.get("created"), result

    wo = WorkOrder.objects.get()
    assert wo.unit == basement["unit"]
    assert wo.property is None, "must not be pinned to one of the three rooms"
    assert wo.area == basement["washroom"]


def test_the_preview_says_who_will_see_it(landlord, basement):
    """The landlord's actual objection was 'the washroom is shared' — so the
    preview has to show that we understood, not just echo a room name."""
    result = _create(
        landlord, property_query="McKenzie Basement", area="Shared Washroom"
    )
    preview = result["preview"]

    assert "Basement" in preview["property"]
    assert "shared" in preview["property"]
    assert preview["area"] == "Shared Washroom"
    for room in ("Room C", "Room D", "Room F"):
        assert room in preview["note"]


def test_a_unit_can_be_named_by_holding_and_unit_together(landlord, basement):
    """"McKenzie Basement" is how a landlord says it — house then floor."""
    result = _create(landlord, property_query="McKenzie Basement", confirm="yes")
    assert result.get("created")
    assert WorkOrder.objects.get().unit == basement["unit"]


def test_a_single_room_fault_still_targets_that_room(landlord, basement):
    """The unit path must not swallow the ordinary case."""
    result = _create(landlord, property_query="Room C", confirm="yes")
    assert result.get("created")

    wo = WorkOrder.objects.get()
    assert wo.property.name == "Room C"
    assert wo.unit is None


def test_an_unrecorded_space_says_so_instead_of_guessing(landlord, basement):
    result = _create(
        landlord, property_query="McKenzie Basement", area="Sauna", confirm="yes"
    )
    assert "error" in result
    assert "hint" in result
    assert not WorkOrder.objects.exists()


def test_an_ambiguous_unit_asks_which_one(landlord, basement):
    other = PropertyHolding.objects.create(
        landlord=landlord, name="3213 Wascana St",
        address="3213 Wascana St", city="Victoria",
    )
    PropertyUnit.objects.create(
        landlord=landlord, holding=other, name="Basement",
        rental_mode=PropertyUnit.RentalMode.WHOLE_UNIT,
    )

    result = _create(landlord, property_query="Basement", confirm="yes")
    assert result.get("candidates")
    assert not WorkOrder.objects.exists()


# ----------------------------------------------- it has the tool, use it
def test_a_described_fault_routes_to_create_work_order():
    """RAMA answered "please open the Maintenance dashboard" for something it
    can do itself, then logged nothing."""
    assert (
        supported_tool_for_request(
            "McKenzie basement washroom's shower was leaking today. The hot "
            "water knob is free/doesn't work and water's leaking."
        )
        == "create_work_order"
    )


@pytest.mark.parametrize(
    "phrasing",
    [
        "the heater is broken in Room D",
        "kitchen tap won't work",
        "there's no hot water upstairs",
        "the basement is flooding",
    ],
)
def test_ordinary_ways_of_reporting_a_fault_are_recognised(phrasing):
    assert supported_tool_for_request(phrasing) == "create_work_order"


# --------------------------------------------- the briefing it denied having
def test_asking_about_morning_updates_is_not_a_capability_gap():
    """Rentium sends a daily 07:00 briefing. RAMA replied "I don't send
    scheduled morning messages" and offered to log a gap for a shipped
    feature, because nothing in its read surface mentioned it."""
    assert (
        supported_tool_for_request("How come no morning updates past few days?")
        == "get_notification_channels"
    )


def test_the_channels_tool_explains_why_briefings_are_not_arriving(landlord):
    out = registry.execute("get_notification_channels", {}, landlord=landlord)
    briefing = out["morning_briefing"]

    assert briefing["exists"] is True
    assert briefing["enabled_on"] == []
    assert "not switched on" in briefing["status"].lower()
    assert briefing["how_to_enable"]


def test_the_briefing_reports_where_it_is_switched_on(landlord):
    from rentium.comms.models import ChannelAccount

    ChannelAccount.objects.create(
        landlord=landlord, channel_type="TELEGRAM", address="1",
        verified=True, prefs={"briefing": True},
    )
    out = registry.execute("get_notification_channels", {}, landlord=landlord)

    assert out["morning_briefing"]["enabled_on"] == ["TELEGRAM"]
    assert "On for TELEGRAM" in out["morning_briefing"]["status"]
