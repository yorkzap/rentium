"""Regression coverage for hierarchical house-layout instructions."""

from __future__ import annotations

from unittest import mock

import pytest

from rentium.properties.models import Property
from rentium.properties.models import PropertyArea
from rentium.properties.models import PropertyGroup
from rentium.properties.models import PropertyHolding
from rentium.rama import registry
from rentium.rama.models import RamaPendingPlan
from rentium.rama.models import RamaPreferences

pytestmark = pytest.mark.django_db
AREA_COUNT = 4
BATHROOM_COUNT = 2
ROOM_COUNT = 3

MCCAUGHEY_REQUEST = (
    "Okay now add another house 5654 mccaugheY street I'll spell that for "
    "and then inside it there's two property groups first one is a basement "
    "for mccaughey and then the second one is the main floor that has three "
    "rooms, two washrooms, one kitchen and living room. One of the washrooms "
    "is private the master bedroom in the main floor and the other washroom "
    "is shared between the other two rooms."
)


class NeverProvider:
    name = "never"
    api_key_setting = "XAI_API_KEY"

    def __init__(self):
        self.requests = []

    def complete(self, **kwargs):
        self.requests.append(kwargs)
        message = "The deterministic house router must not call the model."
        raise AssertionError(message)


def _enable(landlord):
    preferences = RamaPreferences.for_landlord(landlord)
    preferences.enabled = True
    preferences.provider = "xai"
    preferences.api_key = "test-key"
    preferences.save()


def _layout_json():
    from rentium.rama.service import _house_layout_intent

    intent = _house_layout_intent(MCCAUGHEY_REQUEST)
    assert intent is not None
    return intent["arguments"]["layout_json"]


def _complete_arguments():
    return {
        "holding_name": "5654 McCaughey Street",
        "address": "5654 McCaughey Street",
        "city": "Regina",
        "province": "SK",
        "layout_json": _layout_json(),
        "shared_with_landlord": "no",
    }


def test_location_parser_does_not_treat_plain_on_as_ontario():
    from rentium.rama.service import _location_from_text

    assert _location_from_text("The private bathroom is on the main floor") == (
        "",
        "",
    )
    assert _location_from_text("Regina, sk") == ("Regina", "sk")


def test_supplied_house_request_clarifies_once_then_saves_executable_preview(
    landlord,
):
    from rentium.rama.service import run_turn

    _enable(landlord)
    provider = NeverProvider()
    with mock.patch("rentium.rama.service.get_provider", return_value=provider):
        clarification = run_turn(
            landlord,
            MCCAUGHEY_REQUEST,
            role="general",
            channel="telegram",
        )

    assert "I understand the house layout" in clarification.reply
    assert "What city and province is 5654 McCaughey Street in?" in (
        clarification.reply
    )
    assert "Does the landlord or an immediate relative" in clarification.reply
    assert clarification.pending_plan is None
    assert provider.requests == []

    with mock.patch("rentium.rama.service.get_provider", return_value=provider):
        preview = run_turn(
            landlord,
            "It is in Regina, Saskatchewan, and no, the landlord does not use them.",
            clarification.conversation_id,
            role="general",
            channel="telegram",
        )

    assert "Preview: add house 5654 McCaughey Street in Regina, SK" in preview.reply
    assert "McCaughey Basement — empty for now" in preview.reply
    assert "McCaughey Main Floor — 3 room(s)" in preview.reply
    assert "McCaughey Master Bedroom — private areas: Bathroom" in preview.reply
    assert "McCaughey Main Floor Room 2, McCaughey Main Floor Room 3" in (
        preview.reply
    )
    assert "tenant-only" in preview.reply
    assert preview.pending_plan is not None
    plan = RamaPendingPlan.objects.get(conversation_id=preview.conversation_id)
    steps = list(plan.steps.order_by("order"))
    assert len(steps) == 1
    assert steps[0].tool == "create_house_layout"
    assert provider.requests == []


def test_one_yes_creates_exact_private_and_subset_shared_area_hierarchy(landlord):
    from rentium.rama.service import run_turn

    _enable(landlord)
    provider = NeverProvider()
    with mock.patch("rentium.rama.service.get_provider", return_value=provider):
        clarification = run_turn(
            landlord,
            MCCAUGHEY_REQUEST,
            role="general",
            channel="telegram",
        )
        preview = run_turn(
            landlord,
            "Regina SK. The landlord does not use any shared areas.",
            clarification.conversation_id,
            role="general",
            channel="telegram",
        )
        done = run_turn(
            landlord,
            "Yes",
            preview.conversation_id,
            role="general",
            channel="telegram",
        )

    assert "Created house layout for 5654 McCaughey Street" in done.reply
    holding = PropertyHolding.objects.get(
        landlord=landlord,
        name="5654 McCaughey Street",
    )
    basement = PropertyGroup.objects.get(
        landlord=landlord,
        name="McCaughey Basement",
    )
    main = PropertyGroup.objects.get(
        landlord=landlord,
        name="McCaughey Main Floor",
    )
    assert basement.grouped_properties.count() == 0
    rooms = {
        room.name: room
        for room in Property.objects.filter(
            landlord=landlord,
            holding=holding,
            group=main,
        )
    }
    assert set(rooms) == {
        "McCaughey Master Bedroom",
        "McCaughey Main Floor Room 2",
        "McCaughey Main Floor Room 3",
    }
    master = rooms["McCaughey Master Bedroom"]
    room_2 = rooms["McCaughey Main Floor Room 2"]
    room_3 = rooms["McCaughey Main Floor Room 3"]

    bathrooms = list(
        PropertyArea.objects.filter(
            property__group=main,
            area_type=PropertyArea.AreaType.BATHROOM,
        ).prefetch_related("shared_by"),
    )
    assert len(bathrooms) == BATHROOM_COUNT
    memberships = {
        frozenset(area.shared_by.values_list("pk", flat=True)): area
        for area in bathrooms
    }
    assert frozenset({master.pk}) in memberships
    assert frozenset({room_2.pk, room_3.pk}) in memberships
    assert not memberships[frozenset({master.pk})].is_group_common
    assert not memberships[frozenset({room_2.pk, room_3.pk})].is_group_common

    for area_type in (
        PropertyArea.AreaType.KITCHEN,
        PropertyArea.AreaType.LIVING_ROOM,
    ):
        area = PropertyArea.objects.get(
            property__group=main,
            area_type=area_type,
            is_group_common=True,
        )
        assert set(area.shared_by.values_list("pk", flat=True)) == {
            master.pk,
            room_2.pk,
            room_3.pk,
        }
        assert not area.shared_with_landlord
    assert provider.requests == []


def test_house_layout_is_idempotent_and_rolls_back_partial_failure(landlord):
    arguments = _complete_arguments()
    preview = registry.execute(
        "create_house_layout",
        arguments,
        landlord=landlord,
    )
    assert preview["needs_confirm"]

    with mock.patch(
        "rentium.rama.house_layout.PropertyArea.objects.create",
        side_effect=RuntimeError("area write failed"),
    ):
        failed = registry.execute(
            "create_house_layout",
            {**arguments, "confirm": "yes"},
            landlord=landlord,
        )
    assert "nothing was saved" in failed["error"]
    assert not PropertyHolding.objects.filter(landlord=landlord).exists()
    assert not PropertyGroup.objects.filter(landlord=landlord).exists()
    assert not Property.objects.filter(landlord=landlord).exists()

    created = registry.execute(
        "create_house_layout",
        {**arguments, "confirm": "yes"},
        landlord=landlord,
    )
    repeated = registry.execute(
        "create_house_layout",
        {**arguments, "confirm": "yes"},
        landlord=landlord,
    )
    assert created["created_counts"]["rooms"] == ROOM_COUNT
    assert repeated["created_counts"] == {
        "holding": False,
        "groups": 0,
        "rooms": 0,
        "areas": 0,
    }
    assert Property.objects.filter(landlord=landlord).count() == ROOM_COUNT
    assert (
        PropertyArea.objects.filter(property__landlord=landlord).count()
        == AREA_COUNT
    )
