"""Regression tests for RAMA's collected-preview confirmation contract."""

from unittest import mock

import pytest

from rentium.rama import registry
from rentium.rama.models import RamaPendingPlan
from rentium.rama.models import RamaPreferences
from rentium.rama.providers import ToolCall
from rentium.rama.providers import Turn

pytestmark = pytest.mark.django_db
ROOM_COUNT = 5


class ScriptedProvider:
    name = "scripted"
    api_key_setting = "XAI_API_KEY"

    def __init__(self, turns):
        self.turns = list(turns)
        self.requests = []

    def complete(self, *, model, system, messages, tools, api_key=""):
        self.requests.append(
            {
                "model": model,
                "system": system,
                "messages": list(messages),
                "tools": tools,
            },
        )
        return self.turns.pop(0) if self.turns else Turn(text="")


def _enable(landlord):
    preferences = RamaPreferences.for_landlord(landlord)
    preferences.enabled = True
    preferences.provider = "xai"
    preferences.api_key = "test-key"
    preferences.save()


def _room(landlord, name, *, group=None):
    from rentium.properties.models import Property

    return Property.objects.create(
        landlord=landlord,
        group=group,
        name=name,
        address="3213 Wascana St",
        city="Victoria",
        province="bc",
        property_category=Property.PropertyCategory.ROOM,
        room_type=Property.RoomType.PRIVATE,
    )


def test_chained_renames_and_future_name_assignments_run_on_one_yes(landlord):
    """The supplied Wascana failure: future Room 1/2 references resolve to
    Room 4/5 ids, all four previews persist, and one yes runs all four."""
    from rentium.properties.models import PropertyGroup
    from rentium.rama.service import run_turn

    _enable(landlord)
    group = PropertyGroup.objects.create(
        landlord=landlord,
        name="Wascana Main Floor",
    )
    room_4 = _room(landlord, "Room 4")
    room_5 = _room(landlord, "Room 5")
    provider = ScriptedProvider(
        [
            Turn(
                tool_calls=[
                    ToolCall(
                        id="rename-4",
                        name="update_property",
                        arguments={
                            "property_query": "Room 4",
                            "name": "Room 1",
                        },
                    ),
                    ToolCall(
                        id="rename-5",
                        name="update_property",
                        arguments={
                            "property_query": "Room 5",
                            "name": "Room 2",
                        },
                    ),
                    ToolCall(
                        id="group-1",
                        name="assign_property_to_group",
                        arguments={
                            "property_query": "Room 1",
                            "group_name": group.name,
                        },
                    ),
                    ToolCall(
                        id="group-2",
                        name="assign_property_to_group",
                        arguments={
                            "property_query": "Room 2",
                            "group_name": group.name,
                        },
                    ),
                ],
            ),
            Turn(text="I will rename and group the rooms. Confirm yes."),
        ],
    )

    with mock.patch("rentium.rama.service.get_provider", return_value=provider):
        preview = run_turn(
            landlord,
            (
                "Rename Room 4 to Room 1 and Room 5 to Room 2, then assign "
                "Room 1 and Room 2 to Wascana Main Floor."
            ),
            role="general",
            channel="telegram",
        )

    assert "one “Yes” will run all 4 changes" in preview.reply
    assert "Rename Room 4 to Room 1" in preview.reply
    assert "Put Room 1 in Wascana Main Floor" in preview.reply
    plan = RamaPendingPlan.objects.get(conversation_id=preview.conversation_id)
    steps = list(plan.steps.order_by("order"))
    assert [step.tool for step in steps] == [
        "update_property",
        "update_property",
        "assign_property_to_group",
        "assign_property_to_group",
    ]
    assert steps[0].arguments["property_query"] == str(room_4.pk)
    assert steps[2].arguments["property_query"] == str(room_4.pk)
    assert steps[1].arguments["property_query"] == str(room_5.pk)
    assert steps[3].arguments["property_query"] == str(room_5.pk)

    never = ScriptedProvider([Turn(text="must not run")])
    with mock.patch("rentium.rama.service.get_provider", return_value=never):
        done = run_turn(
            landlord,
            "Yes",
            preview.conversation_id,
            role="general",
            channel="telegram",
        )

    room_4.refresh_from_db()
    room_5.refresh_from_db()
    assert (room_4.name, room_4.group_id) == ("Room 1", group.pk)
    assert (room_5.name, room_5.group_id) == ("Room 2", group.pk)
    assert "Renamed Room 4 to Room 1." in done.reply
    assert "Moved Room 1 into Wascana Main Floor." in done.reply
    assert never.requests == []
    assert not RamaPendingPlan.objects.filter(
        conversation_id=preview.conversation_id,
    ).exists()


def test_every_room_creation_previewed_in_one_turn_is_confirmed_together(landlord):
    """The audit showed five previews but only Room 5 was persisted."""
    from rentium.properties.models import Property
    from rentium.properties.models import PropertyGroup
    from rentium.rama.service import run_turn

    _enable(landlord)
    group = PropertyGroup.objects.create(
        landlord=landlord,
        name="Wascana Main Floor",
    )
    calls = [
        ToolCall(
            id=f"create-{number}",
            name="create_property",
            arguments={
                "name": f"Room {number}",
                "address": "3213 Wascana St",
                "city": "Victoria",
                "province": "BC",
                "property_category": "ROOM",
                "group_name": group.name,
            },
        )
        for number in range(1, ROOM_COUNT + 1)
    ]
    provider = ScriptedProvider(
        [
            Turn(tool_calls=calls),
            Turn(text="Confirm yes to create Rooms 1-5."),
        ],
    )
    with mock.patch("rentium.rama.service.get_provider", return_value=provider):
        preview = run_turn(
            landlord,
            "Create Rooms 1 through 5 in Wascana Main Floor.",
            role="general",
            channel="telegram",
        )

    assert preview.pending_plan is not None
    assert len(preview.pending_plan["steps"]) == ROOM_COUNT
    assert "one “Yes” will run all 5 changes" in preview.reply

    with mock.patch(
        "rentium.rama.service.get_provider",
        return_value=ScriptedProvider([]),
    ):
        done = run_turn(
            landlord,
            "Yes",
            preview.conversation_id,
            role="general",
            channel="telegram",
        )

    rooms = list(
        Property.objects.filter(landlord=landlord, group=group)
        .order_by("name")
        .values_list("name", flat=True),
    )
    assert rooms == [f"Room {number}" for number in range(1, ROOM_COUNT + 1)]
    assert done.reply.count("Created Room") == ROOM_COUNT


def test_future_room_name_is_not_reported_missing_when_group_is_already_correct(
    landlord,
):
    """Exact final loop from the transcript: Room 5 is already in the target
    group, then becomes Room 3. The future-name assignment is an id-based
    no-op, not a false 'Room 3 does not exist' blocker."""
    from rentium.properties.models import PropertyGroup
    from rentium.rama.models import RamaAudit
    from rentium.rama.service import run_turn

    _enable(landlord)
    group = PropertyGroup.objects.create(
        landlord=landlord,
        name="Wascana Main Floor",
    )
    room = _room(landlord, "Room 5", group=group)
    provider = ScriptedProvider(
        [
            Turn(
                tool_calls=[
                    ToolCall(
                        id="rename-5",
                        name="update_property",
                        arguments={
                            "property_query": "Room 5",
                            "name": "Room 3",
                        },
                    ),
                    ToolCall(
                        id="group-3",
                        name="assign_property_to_group",
                        arguments={
                            "property_query": "Room 3",
                            "group_name": group.name,
                        },
                    ),
                ],
            ),
            Turn(
                text=(
                    "Rename Room 5 to Room 3, then keep Room 3 in "
                    "Wascana Main Floor. Confirm yes."
                ),
            ),
        ],
    )
    with mock.patch("rentium.rama.service.get_provider", return_value=provider):
        preview = run_turn(
            landlord,
            ("Rename Room 5 to Room 3, then assign Room 3 to Wascana Main Floor."),
            role="general",
            channel="telegram",
        )

    assert "doesn't exist" not in preview.reply
    assert "does not exist" not in preview.reply
    assert len(preview.pending_plan["steps"]) == 1
    tool_rows = RamaAudit.objects.filter(
        conversation_id=preview.conversation_id,
        kind=RamaAudit.Kind.TOOL_CALL,
        content__tool="assign_property_to_group",
    )
    assignment = tool_rows.get().content
    assert assignment["arguments"]["property_query"] == str(room.pk)
    assert assignment["result"]["unchanged"] is True

    with mock.patch(
        "rentium.rama.service.get_provider",
        return_value=ScriptedProvider([]),
    ):
        done = run_turn(
            landlord,
            "Yes",
            preview.conversation_id,
            role="general",
            channel="telegram",
        )
    room.refresh_from_db()
    assert room.name == "Room 3"
    assert room.group_id == group.pk
    assert done.reply == "Renamed Room 5 to Room 3."


def test_model_cannot_confirm_its_own_write_and_repeated_yes_is_safe(landlord):
    from rentium.properties.models import Property
    from rentium.rama.service import run_turn

    _enable(landlord)
    provider = ScriptedProvider(
        [
            Turn(
                tool_calls=[
                    ToolCall(
                        id="unsafe-confirm",
                        name="create_property",
                        arguments={
                            "name": "Model Approved Room",
                            "address": "3213 Wascana St",
                            "city": "Victoria",
                            "confirm": "yes",
                        },
                    ),
                ],
            ),
            Turn(text="Created it."),
        ],
    )
    with mock.patch("rentium.rama.service.get_provider", return_value=provider):
        preview = run_turn(
            landlord,
            "Create Model Approved Room.",
            role="general",
            channel="telegram",
        )
    assert preview.pending_plan is not None
    assert not Property.objects.filter(
        landlord=landlord,
        name="Model Approved Room",
    ).exists()

    with mock.patch(
        "rentium.rama.service.get_provider",
        return_value=ScriptedProvider([]),
    ):
        run_turn(
            landlord,
            "Yes",
            preview.conversation_id,
            role="general",
            channel="telegram",
        )
    assert (
        Property.objects.filter(
            landlord=landlord,
            name="Model Approved Room",
        ).count()
        == 1
    )

    never = ScriptedProvider([Turn(text="must not run")])
    with mock.patch("rentium.rama.service.get_provider", return_value=never):
        repeated = run_turn(
            landlord,
            "Yes",
            preview.conversation_id,
            role="general",
            channel="telegram",
        )
    assert repeated.deterministic is True
    assert "already applied" in repeated.reply
    assert "No action was repeated" in repeated.reply
    assert never.requests == []
    assert (
        Property.objects.filter(
            landlord=landlord,
            name="Model Approved Room",
        ).count()
        == 1
    )


def test_property_name_and_group_noops_do_not_request_confirmation(landlord):
    from rentium.properties.models import PropertyGroup

    group = PropertyGroup.objects.create(
        landlord=landlord,
        name="Wascana Main Floor",
    )
    room = _room(landlord, "Room 2", group=group)

    rename = registry.execute(
        "update_property",
        {"property_query": str(room.pk), "name": "Room 2"},
        landlord=landlord,
    )
    grouping = registry.execute(
        "assign_property_to_group",
        {
            "property_query": str(room.pk),
            "group_name": group.name,
        },
        landlord=landlord,
    )

    assert rename["unchanged"] is True
    assert grouping["unchanged"] is True
    assert "needs_confirm" not in rename
    assert "needs_confirm" not in grouping
    assert "already in Wascana Main Floor" in grouping["message"]


def test_numbered_replacement_rebuilds_real_plan_and_one_yes_creates_all_rooms(
    landlord,
):
    """Replay the six-room Wascana loop.

    "No, make it like this" edits the prior executable plan, retaining its
    location defaults. The returned preview is backed by six persisted steps;
    one Yes creates all six and anchors them to the existing holding.
    """
    import uuid

    from rentium.properties.models import Property
    from rentium.properties.models import PropertyGroup
    from rentium.properties.models import PropertyHolding
    from rentium.rama.models import RamaAudit
    from rentium.rama.plan_runner import save_plan
    from rentium.rama.service import run_turn

    _enable(landlord)
    holding = PropertyHolding.objects.create(
        landlord=landlord,
        name="3213 Wascana St",
        address="3213 Wascana St",
        city="Victoria",
    )
    groups = {
        name: PropertyGroup.objects.create(landlord=landlord, name=name)
        for name in (
            "Wascana Main Floor",
            "Wascana Basement",
            "Wascana Upstairs",
        )
    }
    conversation_id = uuid.uuid4()
    RamaAudit.objects.create(
        landlord=landlord,
        conversation_id=conversation_id,
        kind=RamaAudit.Kind.TOOL_CALL,
        content={
            "tool": "_live_context",
            "arguments": {},
            "result": {
                "rooms": [
                    {
                        "name": f"Old {group_name}",
                        "address": holding.address,
                        "city": holding.city,
                        "province": "bc",
                        "group": group_name,
                    }
                    for group_name in groups
                ]
            },
        },
    )
    old_rows = [
        ("Room 1", "Wascana Main Floor"),
        ("Room 2", "Wascana Main Floor"),
        ("Room Basement", "Wascana Basement"),
        ("Room Upstairs 1", "Wascana Upstairs"),
        ("Room Upstairs 2", "Wascana Upstairs"),
    ]
    save_plan(
        landlord,
        conversation_id,
        {
            "operation": "preview_batch",
            "summary": "Old five-room proposal.",
            "steps": [
                {
                    "tool": "create_property",
                    "target": name,
                    "item_key": f"old:{name}",
                    "arguments": {
                        "name": name,
                        "address": holding.address,
                        # Deliberately stale model-prepared values. The audited
                        # live portfolio above is authoritative.
                        "city": "Regina",
                        "province": "SK",
                        "property_category": "ROOM",
                        "room_type": "PRIVATE",
                        "group_name": group_name,
                    },
                }
                for name, group_name in old_rows
            ],
        },
    )
    correction = """No, make it like this:

1. Create Room 1 in Wascana Main Floor at 3213 Wascana St
2. Create Room 2 in Wascana Main Floor at 3213 Wascana St
3. Create Room Basement in Wascana Basement at 3213 Wascana St
4. Create Room Den Basement in Wascana Basement at 3213 Wascana St
5. Create Room Upstairs 3 in Wascana Upstairs at 3213 Wascana St
6. Create Room Upstairs 4 in Wascana Upstairs at 3213 Wascana St"""
    never = ScriptedProvider([Turn(text="must not run")])
    with mock.patch("rentium.rama.service.get_provider", return_value=never):
        preview = run_turn(
            landlord,
            correction,
            conversation_id,
            role="general",
            channel="telegram",
        )

    assert preview.deterministic is True
    assert never.requests == []
    assert "one “Yes” will run all 6 changes" in preview.reply
    plan = RamaPendingPlan.objects.get(conversation_id=conversation_id)
    assert plan.steps.count() == 6
    assert list(plan.steps.order_by("order").values_list("target_label", flat=True)) == [
        "Room 1",
        "Room 2",
        "Room Basement",
        "Room Den Basement",
        "Room Upstairs 3",
        "Room Upstairs 4",
    ]
    assert {
        (step.arguments["city"], step.arguments["province"])
        for step in plan.steps.all()
    } == {(holding.city, "bc")}

    with mock.patch(
        "rentium.rama.service.get_provider",
        return_value=ScriptedProvider([]),
    ):
        done = run_turn(
            landlord,
            "Yes",
            conversation_id,
            role="general",
            channel="telegram",
        )

    created = list(
        Property.objects.filter(landlord=landlord, address=holding.address)
        .order_by("name")
        .values_list("name", "group__name", "holding_id")
    )
    assert len(created) == 6
    assert {row[2] for row in created} == {holding.pk}
    assert {row[1] for row in created} == set(groups)
    assert done.reply.count("Created Room") == 6
    assert not RamaPendingPlan.objects.filter(
        conversation_id=conversation_id
    ).exists()


def test_model_cannot_turn_needs_input_into_a_fake_confirmable_preview(landlord):
    from rentium.properties.models import PropertyGroup
    from rentium.rama.service import run_turn

    _enable(landlord)
    group = PropertyGroup.objects.create(
        landlord=landlord,
        name="Wascana Main Floor",
    )
    provider = ScriptedProvider(
        [
            Turn(
                tool_calls=[
                    ToolCall(
                        id="empty-group",
                        name="create_group_room",
                        arguments={
                            "name": "Room 1",
                            "group_name": group.name,
                        },
                    )
                ]
            ),
            Turn(
                text=(
                    "Preview — I will create Room 1. "
                    "Reply yes to confirm the complete batch."
                )
            ),
        ]
    )
    with mock.patch("rentium.rama.service.get_provider", return_value=provider):
        result = run_turn(
            landlord,
            "Please handle the requested Wascana setup.",
            role="general",
            channel="telegram",
        )

    assert result.deterministic is True
    assert result.pending_plan is None
    assert "I couldn't prepare an executable preview yet" in result.reply
    assert "Which existing physical holding" in result.reply
    assert "Reply yes" not in result.reply


def test_create_group_room_bootstraps_empty_group_from_exact_holding(landlord):
    from rentium.properties.models import Property
    from rentium.properties.models import PropertyGroup
    from rentium.properties.models import PropertyHolding

    holding = PropertyHolding.objects.create(
        landlord=landlord,
        name="Wascana House",
        address="3213 Wascana St",
        city="Victoria",
    )
    group = PropertyGroup.objects.create(
        landlord=landlord,
        name="Wascana Upstairs",
    )
    arguments = {
        "name": "Room Upstairs 3",
        "group_name": group.name,
        "holding_name": holding.name,
        "province": "bc",
    }
    preview = registry.execute(
        "create_group_room",
        arguments,
        landlord=landlord,
    )
    assert preview["needs_confirm"] is True
    assert preview["preview"]["derived_property_data"] == {
        "address": holding.address,
        "city": holding.city,
        "province": "bc",
        "postal_code": None,
        "country": "Canada",
        "holding": holding.name,
    }

    done = registry.execute(
        "create_group_room",
        {**arguments, "confirm": "yes"},
        landlord=landlord,
    )
    room = Property.objects.get(landlord=landlord, name="Room Upstairs 3")
    assert done["created"] is True
    assert room.group_id == group.pk
    assert room.holding_id == holding.pk
    assert room.address == holding.address
