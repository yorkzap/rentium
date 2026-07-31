"""End-to-end regressions for the conversation that exposed RAMA's weak spots."""

from __future__ import annotations

import json
from datetime import datetime
from decimal import Decimal
from unittest import mock
from zoneinfo import ZoneInfo

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from rest_framework.test import APIClient

from rentium.appointments.models import Appointment
from rentium.leases.models import Lease
from rentium.leases.models import LeaseInviteEvent
from rentium.leases.models import LeaseTenant
from rentium.properties.models import Property
from rentium.properties.models import PropertyArea
from rentium.properties.models import PropertyHolding
from rentium.properties.models import PropertyImage
from rentium.properties.models import PropertyUnit
from rentium.rama import registry
from rentium.rama.models import RamaCapabilityGap
from rentium.rama.models import RamaPendingPlan
from rentium.rama.models import RamaPreferences

pytestmark = pytest.mark.django_db
TINY_GIF = (
    b"GIF89a\x01\x00\x01\x00\x80\x00\x00\x00\x00\x00\xff\xff\xff!\xf9\x04"
    b"\x01\x00\x00\x00\x00,\x00\x00\x00\x00\x01\x00\x01\x00\x00\x02\x02D\x01\x00;"
)


def _room(landlord, name: str, address: str) -> Property:
    return Property.objects.create(
        landlord=landlord,
        name=name,
        address=address,
        city="Victoria",
        province="bc",
        property_category=Property.PropertyCategory.ROOM,
        room_type=Property.RoomType.PRIVATE,
        status=Property.PropertyStatus.AVAILABLE,
    )


def _enable_rama(landlord):
    preferences = RamaPreferences.for_landlord(landlord)
    preferences.enabled = True
    preferences.provider = "xai"
    preferences.api_key = "test-key"
    preferences.save()


def _image(name: str):
    return SimpleUploadedFile(name, TINY_GIF, content_type="image/gif")


def test_mckenzie_suite_layout_keeps_exact_names_and_all_areas(landlord):
    from rentium.rama.service import run_turn

    _enable_rama(landlord)
    holding = PropertyHolding.objects.create(
        landlord=landlord,
        name="McKenzie House",
        address="950 McKenzie Ave",
        city="Victoria",
    )
    unit = PropertyUnit.objects.create(
        landlord=landlord,
        holding=holding,
        name="Garden Suite",
        unit_type=PropertyUnit.UnitType.GARDEN_SUITE,
        rental_mode=PropertyUnit.RentalMode.WHOLE_UNIT,
    )
    whole = Property.objects.create(
        landlord=landlord,
        holding=holding,
        unit=unit,
        name="McKenzie Garden Suite",
        address="950 McKenzie Ave",
        city="Victoria",
        province="bc",
        property_category=Property.PropertyCategory.COMPLETE_UNIT,
        unit_type=Property.UnitType.GARDEN_SUITE,
    )
    provider = mock.Mock()

    with mock.patch("rentium.rama.service.get_provider", return_value=provider):
        preview = run_turn(
            landlord,
            (
                "Can u add two rooms into the mckenzie ave garden suite? "
                'That suite has one washroom, one kitchen, one bonus room "J", '
                'one private room "K" and a patio area.'
            ),
        )

    assert preview.deterministic is True
    assert provider.complete.call_count == 0
    assert "Bonus room J" in preview.reply
    assert "Room K" in preview.reply
    assert "Washroom" in preview.reply
    assert "Kitchen" in preview.reply
    assert "Patio" in preview.reply
    assert "Room L" not in preview.reply
    assert "Room M" not in preview.reply
    plan = RamaPendingPlan.objects.get(conversation_id=preview.conversation_id)
    step = plan.steps.get()
    assert step.tool == "configure_unit_room_offerings"
    assert step.arguments["unit_name"] == str(unit.pk)

    with mock.patch("rentium.rama.service.get_provider", return_value=provider):
        completed = run_turn(landlord, "yes", preview.conversation_id)

    unit.refresh_from_db()
    whole.refresh_from_db()
    assert completed.deterministic is True
    assert unit.rental_mode == PropertyUnit.RentalMode.BY_ROOM
    assert whole.is_active_offering is False
    assert set(
        unit.active_offerings().values_list("name", flat=True)
    ) == {"Bonus room J", "Room K"}
    assert unit.room_group.name == "McKenzie Garden Suite"
    assert set(
        PropertyArea.objects.filter(
            property__group=unit.room_group,
            is_group_common=True,
        ).values_list("name", flat=True)
    ) == {"Washroom", "Kitchen", "Patio"}
    assert not RamaCapabilityGap.objects.filter(
        landlord=landlord,
        request__icontains="McKenzie Garden Suite",
    ).exists()


def test_mckenzie_garden_suite_wins_over_same_named_suite_elsewhere(landlord):
    """Two 'Garden Suite' units must not fall through to the model.

    The live failure invented Bonus room L/M, tried to create a non-existent
    property group, then failed plan execution with 'Several units match'.
    Street evidence in the message must pin the McKenzie unit and run the
    atomic configure tool with the landlord's exact room names.
    """
    from rentium.rama.service import run_turn

    _enable_rama(landlord)
    mck = PropertyHolding.objects.create(
        landlord=landlord,
        name="McKenzie House",
        address="950 McKenzie Ave",
        city="Victoria",
    )
    other = PropertyHolding.objects.create(
        landlord=landlord,
        name="Other House",
        address="100 Other St",
        city="Victoria",
    )
    mck_unit = PropertyUnit.objects.create(
        landlord=landlord,
        holding=mck,
        name="Garden Suite",
        unit_type=PropertyUnit.UnitType.GARDEN_SUITE,
        rental_mode=PropertyUnit.RentalMode.WHOLE_UNIT,
    )
    PropertyUnit.objects.create(
        landlord=landlord,
        holding=other,
        name="Garden Suite",
        unit_type=PropertyUnit.UnitType.GARDEN_SUITE,
        rental_mode=PropertyUnit.RentalMode.WHOLE_UNIT,
    )
    # Sibling BY_ROOM units at the same address must not be dragged into a
    # "switch every unit" plan when the landlord named the garden suite.
    for name in ("Basement", "Upstairs"):
        PropertyUnit.objects.create(
            landlord=landlord,
            holding=mck,
            name=name,
            rental_mode=PropertyUnit.RentalMode.BY_ROOM,
        )
    Property.objects.create(
        landlord=landlord,
        holding=mck,
        unit=mck_unit,
        name="Garden Suite",
        address="950 McKenzie Ave",
        city="Victoria",
        province="bc",
        property_category=Property.PropertyCategory.COMPLETE_UNIT,
        unit_type=Property.UnitType.GARDEN_SUITE,
    )
    provider = mock.Mock()

    with mock.patch("rentium.rama.service.get_provider", return_value=provider):
        preview = run_turn(
            landlord,
            (
                "Can u add two rooms into the mckenzie ave garden suite? "
                'that suite has a one washroom, one kitchen, one bonus room "J", '
                'one private room "K" and a patio area'
            ),
        )

    assert preview.deterministic is True
    assert provider.complete.call_count == 0
    assert "Bonus room J" in preview.reply
    assert "Room K" in preview.reply
    assert "Room L" not in preview.reply
    assert "Room M" not in preview.reply
    assert "capability gap" not in preview.reply.casefold()
    plan = RamaPendingPlan.objects.get(conversation_id=preview.conversation_id)
    step = plan.steps.get()
    assert step.tool == "configure_unit_room_offerings"
    assert step.arguments["unit_name"] == str(mck_unit.pk)

    with mock.patch("rentium.rama.service.get_provider", return_value=provider):
        done = run_turn(landlord, "yes", preview.conversation_id)

    mck_unit.refresh_from_db()
    assert done.deterministic is True
    assert "Several units match" not in (done.reply or "")
    assert mck_unit.rental_mode == PropertyUnit.RentalMode.BY_ROOM
    assert set(
        mck_unit.active_offerings().values_list("name", flat=True)
    ) == {"Bonus room J", "Room K"}
    # Group link or public room links — never app.rentium.ca.
    assert "app.rentium.ca" not in (done.reply or "")
    assert "view-group" in (done.reply or "") or "Bonus room J" in (done.reply or "")


def test_switch_rental_mode_plan_executes_with_unit_uuid_not_bare_name(landlord):
    """Confirmed mode switches must not re-resolve by ambiguous name."""
    import uuid as uuid_mod

    from rentium.rama.plan_runner import run_plan
    from rentium.rama.plan_runner import save_single

    _enable_rama(landlord)
    mck = PropertyHolding.objects.create(
        landlord=landlord,
        name="McKenzie House",
        address="950 McKenzie Ave",
        city="Victoria",
    )
    was = PropertyHolding.objects.create(
        landlord=landlord,
        name="Wascana",
        address="3213 Wascana St",
        city="Victoria",
    )
    mck_unit = PropertyUnit.objects.create(
        landlord=landlord,
        holding=mck,
        name="Garden Suite",
        rental_mode=PropertyUnit.RentalMode.WHOLE_UNIT,
    )
    PropertyUnit.objects.create(
        landlord=landlord,
        holding=was,
        name="Garden Suite",
        rental_mode=PropertyUnit.RentalMode.WHOLE_UNIT,
    )
    Property.objects.create(
        landlord=landlord,
        holding=mck,
        unit=mck_unit,
        name="Garden Suite",
        address="950 McKenzie Ave",
        city="Victoria",
        province="bc",
        property_category=Property.PropertyCategory.COMPLETE_UNIT,
    )

    planned = registry.execute(
        "plan_operation",
        {
            "operation": "switch_rental_mode",
            "new_mode": "BY_ROOM",
            "include": "Garden Suite",
            "holding": "950 McKenzie",
        },
        landlord=landlord,
    )
    assert planned.get("needs_confirm"), planned
    step = planned["plan"]["steps"][0]
    assert step["arguments"]["unit_name"] == str(mck_unit.pk)
    # Bare name must not reappear as the only identifier.
    assert step["arguments"]["unit_name"] != "Garden Suite"

    plan = save_single(
        landlord,
        conversation_id=uuid_mod.uuid4(),
        tool=step["tool"],
        arguments=step["arguments"],
    )
    progress = run_plan(plan, landlord)
    assert not progress.get("failed"), progress
    mck_unit.refresh_from_db()
    assert mck_unit.rental_mode == PropertyUnit.RentalMode.BY_ROOM


def test_rama_shows_numbered_thumbnails_then_removes_exact_selection(landlord):
    from rentium.rama.service import run_turn

    _enable_rama(landlord)
    listing = Property.objects.create(
        landlord=landlord,
        name="McKenzie Garden Suite",
        address="950 McKenzie Ave",
        city="Victoria",
        province="bc",
        property_category=Property.PropertyCategory.COMPLETE_UNIT,
        unit_type=Property.UnitType.GARDEN_SUITE,
        primary_image=_image("main.gif"),
    )
    first = PropertyImage.objects.create(
        property=listing,
        image=_image("mortgage.gif"),
        order=0,
    )
    second = PropertyImage.objects.create(
        property=listing,
        image=_image("regina-basement.gif"),
        order=1,
    )
    provider = mock.Mock()

    with mock.patch("rentium.rama.service.get_provider", return_value=provider):
        shown = run_turn(
            landlord,
            "Delete some images from McKenzie Garden Suite.",
        )

    assert shown.deterministic is True
    assert "1. main photo" in shown.reply
    assert "2. gallery" in shown.reply
    assert shown.attachments[0]["kind"] == "property_media"
    assert len(shown.attachments[0]["media"]) == 3
    assert provider.complete.call_count == 0

    with mock.patch("rentium.rama.service.get_provider", return_value=provider):
        preview = run_turn(
            landlord,
            "Remove photos 2 and 3.",
            shown.conversation_id,
        )
    assert "remove 2 exact photo(s)" in preview.reply
    plan = RamaPendingPlan.objects.get(conversation_id=shown.conversation_id)
    step = plan.steps.get()
    assert step.tool == "remove_photos_from_listing"
    assert set(json.loads(step.arguments["media_handles_json"])) == {
        f"gallery:{first.pk}",
        f"gallery:{second.pk}",
    }

    with mock.patch("rentium.rama.service.get_provider", return_value=provider):
        run_turn(landlord, "yes", shown.conversation_id)

    listing.refresh_from_db()
    assert listing.primary_image
    assert not listing.property_images.exists()


def test_room_d_lease_is_drafted_before_siya_is_invited(landlord):
    room = _room(landlord, "McKenzie Room D", "950 McKenzie Ave")
    terms = (
        "The Roommate agrees to rent a private basement bedroom located at "
        "950 McKenzie Ave, Victoria, BC V8X 3G5. Common areas—including the "
        "kitchen, washroom, living room, and entryway—shall be shared with the "
        "landlord(s) and/or the landlord’s immediate family (son/daughter)."
    )

    created = registry.execute(
        "create_lease",
        {
            "property_query": room.name,
            "start_date": "2026-08-01",
            "end_date": "2026-12-31",
            "total_rent": "800",
            "security_deposit": "400",
            "pet_deposit": "0",
            "cleaning_fee": "0",
            "special_terms": terms,
            "confirm": "yes",
        },
        landlord=landlord,
    )
    assert created.get("created"), created
    lease = Lease.objects.get(pk=created["lease"]["id"])
    assert lease.status == Lease.LeaseStatus.DRAFT
    assert lease.total_rent == Decimal("800.00")
    assert lease.security_deposit == Decimal("400.00")
    assert lease.start_date.isoformat() == "2026-08-01"
    assert lease.end_date.isoformat() == "2026-12-31"
    assert lease.special_terms == terms
    assert not lease.lease_tenants.exists()

    with mock.patch(
        "rentium.showcase.emails.send_tenant_invite",
        return_value=True,
    ):
        invited = registry.execute(
            "invite_tenant_to_lease",
            {
                "lease_number": lease.lease_number,
                "name": "Siya Gulati",
                "email": "siyagulati900@gmail.com",
                "rent_amount": "800",
                "confirm": "yes",
            },
            landlord=landlord,
        )
    assert invited.get("invited"), invited
    slot = LeaseTenant.objects.get(lease=lease)
    assert slot.invited_name == "Siya Gulati"
    assert slot.invited_email == "siyagulati900@gmail.com"
    assert slot.rent_amount == Decimal("800.00")
    assert slot.invite_events.filter(kind=LeaseInviteEvent.Kind.SENT).exists()


def test_invite_open_account_and_signature_are_distinct_facts(landlord):
    room = _room(landlord, "McKenzie Room D", "950 McKenzie Ave")
    lease = Lease.objects.create(
        landlord=landlord,
        property=room,
        lease_type=Lease.LeaseType.GENERIC_ROOMMATE,
        status=Lease.LeaseStatus.PENDING_SIGNATURES,
        start_date="2026-08-01",
        end_date="2026-12-31",
        is_month_to_month=False,
        total_rent="800.00",
    )
    slot = LeaseTenant.objects.create(
        lease=lease,
        invited_name="Siya Gulati",
        invited_email="siyagulati900@gmail.com",
        rent_amount="800.00",
    )

    before = registry.execute(
        "list_lease_roster",
        {"lease_number": lease.lease_number},
        landlord=landlord,
    )["active_tenants"][0]["invite_lifecycle"]
    assert before["invite_link_opened"] is False
    assert before["account_linked"] is False
    assert before["signed"] is False

    response = APIClient().get(
        f"/api/leases/tenants/{slot.pk}/invite-preview/",
        {"token": str(slot.invite_token)},
        HTTP_USER_AGENT="Regression test browser",
    )
    assert response.status_code == 200
    after_open = registry.execute(
        "list_lease_roster",
        {"lease_number": lease.lease_number},
        landlord=landlord,
    )["active_tenants"][0]["invite_lifecycle"]
    assert after_open["invite_link_opened"] is True
    assert after_open["account_linked"] is False
    assert after_open["signed"] is False
    assert "not proof" in after_open["evidence_note"]


def test_viewing_uses_mckenzie_garden_suite_not_wascana(landlord):
    mckenzie = Property.objects.create(
        landlord=landlord,
        name="McKenzie Garden Suite",
        address="950 McKenzie Ave",
        city="Victoria",
        province="bc",
        property_category=Property.PropertyCategory.COMPLETE_UNIT,
        unit_type=Property.UnitType.GARDEN_SUITE,
    )
    Property.objects.create(
        landlord=landlord,
        name="Wascana Garden Suite",
        address="3213 Wascana St",
        city="Victoria",
        province="bc",
        property_category=Property.PropertyCategory.COMPLETE_UNIT,
        unit_type=Property.UnitType.GARDEN_SUITE,
    )
    result = registry.execute(
        "schedule_viewing",
        {
            "property_query": "McKenzie Garden Suite",
            "when": "2026-07-31 14:00",
            "contact_name": "Hitakshi Verma",
            "contact_email": "Hitakshiverma01@gmail.com",
            "confirm": "yes",
        },
        landlord=landlord,
    )
    assert result.get("created"), result
    appointment = Appointment.objects.get(pk=result["appointment"]["id"])
    assert appointment.property == mckenzie
    assert appointment.contact_name == "Hitakshi Verma"
    assert appointment.starts_at.astimezone(
        ZoneInfo("America/Vancouver"),
    ) == datetime(2026, 7, 31, 14, 0, tzinfo=ZoneInfo("America/Vancouver"))


def test_public_listing_link_uses_canonical_slug_route(landlord):
    from rentium.showcase.models import Showcase

    listing = Property.objects.create(
        landlord=landlord,
        name="McKenzie Garden Suite",
        address="950 McKenzie Ave",
        city="Victoria",
        province="bc",
        property_category=Property.PropertyCategory.COMPLETE_UNIT,
        unit_type=Property.UnitType.GARDEN_SUITE,
        is_publicly_visible=True,
        status=Property.PropertyStatus.AVAILABLE,
    )
    Showcase.objects.create(
        landlord=landlord,
        slug="mckenzie-rentals",
        is_public=True,
    )

    result = registry.execute(
        "public_property_link",
        {"property_query": listing.name},
        landlord=landlord,
    )
    assert result["link"] == (
        f"https://www.rentium.ca/bc/victoria/{listing.public_slug}"
    )
    assert "/properties/" not in result["link"]
    assert "%20" not in result["link"]


def test_rest_and_rama_lease_creation_share_legal_derivation(landlord):
    rama_room = _room(landlord, "RAMA Room", "950 McKenzie Ave")
    api_room = _room(landlord, "API Room", "950 McKenzie Ave")
    for room in (rama_room, api_room):
        PropertyArea.objects.create(
            property=room,
            name="Kitchen",
            area_type=PropertyArea.AreaType.KITCHEN,
            kind=PropertyArea.Kind.COMMON,
            shared_with_landlord=True,
        )

    rama_result = registry.execute(
        "create_lease",
        {
            "property_query": rama_room.name,
            "start_date": "2026-08-01",
            "end_date": "2026-12-31",
            "total_rent": "800",
            "confirm": "yes",
        },
        landlord=landlord,
    )
    rama_lease = Lease.objects.get(pk=rama_result["lease"]["id"])

    client = APIClient()
    client.force_authenticate(user=landlord.user)
    response = client.post(
        "/api/leases/",
        {
            "property_id": str(api_room.pk),
            "lease_type": Lease.LeaseType.GENERIC_ROOMMATE,
            "status": Lease.LeaseStatus.DRAFT,
            "start_date": "2026-08-01",
            "end_date": "2026-12-31",
            "is_month_to_month": False,
            "total_rent": "800.00",
            "security_deposit": "400.00",
            "pet_deposit": "0.00",
            "cleaning_fee": "0.00",
        },
        format="json",
    )
    assert response.status_code == 201, response.data
    api_lease = Lease.objects.get(pk=response.data["id"])
    assert rama_lease.common_space_shared_with == ["LANDLORD"]
    assert api_lease.common_space_shared_with == ["LANDLORD"]
