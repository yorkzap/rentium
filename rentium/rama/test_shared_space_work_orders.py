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


# ------------------------------------------------- the job it couldn't close
# Follow-up from the same landlord, one day later:
#
#     landlord: I fixed it by buying and putting a new knob
#     RAMA:     Should I mark it as completed since you fixed it?
#     landlord: Yes ... the amount was 19.78
#     RAMA:     Created 84ab93ba-...
#     landlord: i see 0 work orders
#
# The ticket existed. Three things hid it and stopped it closing.

def test_a_shared_space_job_is_visible_to_its_landlord(landlord, basement):
    """It was filed, then invisible: every landlord query scoped work orders
    through `property__landlord`, and a shared-space job has no property."""
    _create(landlord, property_query="McKenzie Basement", confirm="yes")

    assert WorkOrder.objects.for_landlord(landlord).count() == 1


def test_scoping_still_excludes_other_landlords(landlord, basement):
    from rentium.users.models import LandlordProfile
    from rentium.users.tests.factories import UserFactory

    _create(landlord, property_query="McKenzie Basement", confirm="yes")
    stranger = LandlordProfile.objects.create(user=UserFactory())

    assert WorkOrder.objects.for_landlord(stranger).count() == 0


def test_a_shared_space_job_can_be_found_again_to_complete_it(landlord, basement):
    """"Mark it completed" silently did nothing because _resolve_work_order
    filtered on property__landlord and could never find the job RAMA had just
    created."""
    created = _create(
        landlord, property_query="McKenzie Basement", confirm="yes"
    )
    wo_id = created["work_order"]["id"]

    result = registry.execute(
        "complete_work_order",
        {"work_order_id": wo_id, "cost": "19.78", "confirm": "yes"},
        landlord=landlord,
    )
    assert result.get("completed"), result

    wo = WorkOrder.objects.get()
    assert wo.status == WorkOrder.Status.COMPLETED
    assert str(wo.cost) == "19.78"


def test_a_job_the_landlord_already_fixed_can_be_closed_directly(
    landlord, basement
):
    """NEW -> COMPLETED. The commonest small repair is "it broke and I fixed
    it myself", logged after the fact; requiring SCHEDULED first left finished
    repairs sitting open forever."""
    created = _create(
        landlord, property_query="McKenzie Basement", confirm="yes"
    )
    wo = WorkOrder.objects.get(pk=created["work_order"]["id"])
    assert wo.status == WorkOrder.Status.NEW

    wo.transition_to(WorkOrder.Status.COMPLETED)
    assert wo.status == WorkOrder.Status.COMPLETED


def test_completed_is_still_terminal(landlord, basement):
    """Relaxing the entry into COMPLETED must not make it reopenable."""
    from django.core.exceptions import ValidationError

    created = _create(
        landlord, property_query="McKenzie Basement", confirm="yes"
    )
    wo = WorkOrder.objects.get(pk=created["work_order"]["id"])
    wo.transition_to(WorkOrder.Status.COMPLETED)

    with pytest.raises(ValidationError):
        wo.transition_to(WorkOrder.Status.IN_PROGRESS)


def test_a_shared_space_expense_is_booked_against_the_address(
    landlord, basement
):
    """No listing to charge, but there IS an address. Without the holding the
    expense lands nowhere and cannot be attributed to the house."""
    from rentium.ledger.models import LedgerEntry

    created = _create(
        landlord, property_query="McKenzie Basement", confirm="yes"
    )
    registry.execute(
        "complete_work_order",
        {"work_order_id": created["work_order"]["id"], "cost": "19.78",
         "post_expense": "1", "confirm": "yes"},
        landlord=landlord,
    )

    expense = LedgerEntry.objects.get(entry_type="EXPENSE")
    assert expense.property is None, "not charged to one of the shared rooms"
    assert expense.holding == basement["unit"].holding
    assert expense.work_order_id is not None, "linked back to the job"


def test_completing_twice_does_not_double_post_the_expense(landlord, basement):
    """The idempotency key is what stops a $19.78 repair becoming $39.56."""
    from rentium.ledger.models import LedgerEntry

    created = _create(
        landlord, property_query="McKenzie Basement", confirm="yes"
    )
    args = {
        "work_order_id": created["work_order"]["id"], "cost": "19.78",
        "post_expense": "1", "confirm": "yes",
    }
    registry.execute("complete_work_order", args, landlord=landlord)
    registry.execute("complete_work_order", args, landlord=landlord)

    assert LedgerEntry.objects.filter(entry_type="EXPENSE").count() == 1
