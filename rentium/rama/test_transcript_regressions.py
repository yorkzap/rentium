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


def test_expense_paid_correction_replaces_stale_preview_and_followups_are_grounded(
    landlord,
):
    """Regression: "Yes, except it's taken" must not post the unpaid plan.

    Paid-status questions are reads about the just-written entry, not fuzzy
    searches for descriptions containing punctuation or pronouns.
    """
    import uuid
    from datetime import date

    from rentium.ledger.models import EntryType
    from rentium.ledger.models import LedgerEntry
    from rentium.rama.plan_runner import save_single
    from rentium.rama.service import run_turn

    _enable_rama(landlord)
    holding = PropertyHolding.objects.create(
        landlord=landlord,
        name="950 McKenzie Ave",
        address="950 McKenzie Ave",
        city="Victoria",
    )
    conversation_id = uuid.uuid4()
    args = {
        "amount": "280.00",
        "description": (
            "Stove for McKenzie Basement + disposal of old unit - "
            "Chris Klatt's Second Hand Appliances"
        ),
        "holding_name": "950 McKenzie Ave",
        "property_query": "",
        "effective_date": "2026-08-02",
        "paid_on": "",
        "category": "OTHER",
    }
    save_single(landlord, conversation_id, "create_expense", args)
    provider = mock.Mock()

    with mock.patch("rentium.rama.service.get_provider", return_value=provider):
        pending_status = run_turn(
            landlord,
            "did u mark it paid",
            conversation_id,
        )

    assert "Not yet — the expense has not been posted" in pending_status.reply
    assert RamaPendingPlan.objects.get(
        conversation_id=conversation_id,
    ).steps.get().arguments["paid_on"] == ""

    with mock.patch("rentium.rama.service.get_provider", return_value=provider):
        corrected = run_turn(
            landlord,
            "Yes, except for it's taken from the bank already",
            conversation_id,
        )

    assert corrected.deterministic is True
    assert provider.complete.call_count == 0
    assert "Bank: paid" in corrected.reply
    assert not LedgerEntry.objects.filter(
        landlord=landlord,
        entry_type=EntryType.EXPENSE,
    ).exists()
    plan = RamaPendingPlan.objects.get(conversation_id=conversation_id)
    step = plan.steps.get()
    assert step.tool == "create_expense"
    assert step.arguments["paid_on"] == "paid"

    with mock.patch("rentium.rama.service.get_provider", return_value=provider):
        posted = run_turn(landlord, "yes", conversation_id)

    entries = LedgerEntry.objects.filter(
        landlord=landlord,
        entry_type=EntryType.EXPENSE,
    )
    assert entries.count() == 1
    entry = entries.get()
    assert entry.holding_id == holding.pk
    assert entry.paid_on == date(2026, 8, 2)
    assert "paid 2026-08-02" in posted.reply
    assert "receipt" in posted.reply.casefold()

    with mock.patch("rentium.rama.service.get_provider", return_value=provider):
        terse_status = run_turn(landlord, "marked paid?", conversation_id)
        natural_status = run_turn(landlord, "did u mark it paid", conversation_id)

    assert provider.complete.call_count == 0
    assert "Yes — the $280.00 expense" in terse_status.reply
    assert "marked paid on 2026-08-02" in terse_status.reply
    assert "Yes — the $280.00 expense" in natural_status.reply
    assert entries.count() == 1


@pytest.mark.parametrize("command", ["mark it paid", "then mark it paid"])
def test_mark_it_paid_uses_recent_expense_id_not_pronoun_search(
    landlord,
    command,
):
    import uuid

    from rentium.ledger.models import LedgerEntry
    from rentium.rama.plan_runner import save_single
    from rentium.rama.service import run_turn

    _enable_rama(landlord)
    PropertyHolding.objects.create(
        landlord=landlord,
        name="950 McKenzie Ave",
        address="950 McKenzie Ave",
        city="Victoria",
    )
    conversation_id = uuid.uuid4()
    save_single(
        landlord,
        conversation_id,
        "create_expense",
        {
            "amount": "280.00",
            "description": "Replacement stove",
            "holding_name": "950 McKenzie Ave",
            "paid_on": "",
        },
    )
    provider = mock.Mock()
    with mock.patch("rentium.rama.service.get_provider", return_value=provider):
        run_turn(landlord, "yes", conversation_id)
        mark_preview = run_turn(landlord, command, conversation_id)

    entry = LedgerEntry.objects.get(landlord=landlord)
    assert entry.paid_on is None
    assert "Mark expense paid:" in mark_preview.reply
    plan = RamaPendingPlan.objects.get(conversation_id=conversation_id)
    step = plan.steps.get()
    assert step.tool == "mark_ledger_paid"
    assert step.arguments["entry_id"] == str(entry.pk)
    assert step.arguments["description_query"] == ""
    assert provider.complete.call_count == 0


def test_then_do_it_after_unpaid_status_previews_and_confirms_mark_paid(landlord):
    import uuid

    from rentium.ledger.models import LedgerEntry
    from rentium.rama.plan_runner import save_single
    from rentium.rama.service import run_turn

    _enable_rama(landlord)
    PropertyHolding.objects.create(
        landlord=landlord,
        name="950 McKenzie Ave",
        address="950 McKenzie Ave",
        city="Victoria",
    )
    conversation_id = uuid.uuid4()
    save_single(
        landlord,
        conversation_id,
        "create_expense",
        {
            "amount": "280.00",
            "description": "Replacement stove",
            "holding_name": "950 McKenzie Ave",
            "paid_on": "",
        },
    )
    provider = mock.Mock()
    with mock.patch("rentium.rama.service.get_provider", return_value=provider):
        run_turn(landlord, "yes", conversation_id)
        status = run_turn(landlord, "marked paid?", conversation_id)
        mark_preview = run_turn(landlord, "then do it", conversation_id)

    entry = LedgerEntry.objects.get(landlord=landlord)
    assert "still marked not yet taken from bank" in status.reply
    assert "Mark expense paid:" in mark_preview.reply
    plan = RamaPendingPlan.objects.get(conversation_id=conversation_id)
    assert plan.steps.get().arguments["entry_id"] == str(entry.pk)
    assert entry.paid_on is None

    with mock.patch("rentium.rama.service.get_provider", return_value=provider):
        done = run_turn(landlord, "then do it", conversation_id)

    entry.refresh_from_db()
    assert entry.paid_on is not None
    assert "marked paid" in done.reply
    assert provider.complete.call_count == 0


def test_new_multi_line_expense_beats_stale_document_focus_and_totals_costs(
    landlord,
):
    import uuid

    from rentium.rama.document_services import ingest_document
    from rentium.rama.models import RamaAudit
    from rentium.rama.plan_runner import save_single
    from rentium.rama.service import run_turn

    _enable_rama(landlord)
    holding = PropertyHolding.objects.create(
        landlord=landlord,
        name="950 McKenzie Ave",
        address="950 McKenzie Ave",
        city="Victoria",
    )
    Property.objects.create(
        landlord=landlord,
        holding=holding,
        name="McKenzie Basement",
        address="950 McKenzie Ave",
        city="Victoria",
        province="bc",
        property_category=Property.PropertyCategory.COMPLETE_UNIT,
    )
    old_document, _ = ingest_document(
        landlord=landlord,
        upload=SimpleUploadedFile(
            "older-document.pdf",
            b"%PDF-old-unresolved-document",
            content_type="application/pdf",
        ),
    )
    conversation_id = uuid.uuid4()
    RamaAudit.objects.create(
        landlord=landlord,
        conversation_id=conversation_id,
        kind=RamaAudit.Kind.TOOL_CALL,
        content={
            "tool": "catalog_business_document",
            "arguments": {"document_id": str(old_document.pk)},
            "result": {
                "prepared": True,
                "document_id": str(old_document.pk),
                "needs_scope": True,
            },
        },
    )
    save_single(
        landlord,
        conversation_id,
        "catalog_business_document",
        {
            "document_id": str(old_document.pk),
            "scope_query": "950 McKenzie Ave",
        },
    )
    provider = mock.Mock()
    message = (
        "what did u make? i bought a stove for mckenzie basement for $250 and "
        "$30 for dumping the old one. Used Chris Klatt's second hand appliances."
    )

    with mock.patch("rentium.rama.service.get_provider", return_value=provider):
        preview = run_turn(landlord, message, conversation_id)

    assert preview.deterministic is True
    assert provider.complete.call_count == 0
    assert "Expense to file (no receipt required):" in preview.reply
    assert "• Amount: $280.00" in preview.reply
    assert "• Property: 950 McKenzie Ave — whole property" in preview.reply
    assert "$250" in preview.reply and "$30" in preview.reply
    plan = RamaPendingPlan.objects.get(conversation_id=conversation_id)
    step = plan.steps.get()
    assert step.tool == "create_expense"
    assert step.arguments["amount"] == "280.00"
    assert step.arguments["holding_name"] == "950 McKenzie Ave"
    assert step.arguments["paid_on"] == ""
    with mock.patch("rentium.rama.service.get_provider", return_value=provider):
        done = run_turn(landlord, "yes", conversation_id)

    assert "Logged $280.00" in done.reply
    old_document.refresh_from_db()
    assert old_document.holding_id is None
    assert old_document.ledger_entry_id is None


def test_no_receipt_correction_recovers_paid_purchase_and_replaces_document_plan(
    landlord,
):
    import uuid

    from rentium.ledger.models import EntryType
    from rentium.ledger.models import LedgerEntry
    from rentium.rama.document_services import ingest_document
    from rentium.rama.models import RamaAudit
    from rentium.rama.service import run_turn

    _enable_rama(landlord)
    holding = PropertyHolding.objects.create(
        landlord=landlord,
        name="950 McKenzie Ave",
        address="950 McKenzie Ave",
        city="Victoria",
    )
    Property.objects.create(
        landlord=landlord,
        holding=holding,
        name="McKenzie Basement",
        address="950 McKenzie Ave",
        city="Victoria",
        province="bc",
        property_category=Property.PropertyCategory.COMPLETE_UNIT,
    )
    old_document, _ = ingest_document(
        landlord=landlord,
        upload=SimpleUploadedFile(
            "wrong-document.pdf",
            b"%PDF-must-not-be-filed",
            content_type="application/pdf",
        ),
    )
    conversation_id = uuid.uuid4()
    purchase = (
        "what did u make? i bought a stove for mckenzie basement for $250 and "
        "gave him another $30 to dump the old stove from Chris Klatt second "
        "hand appliances"
    )
    RamaAudit.objects.create(
        landlord=landlord,
        conversation_id=conversation_id,
        kind=RamaAudit.Kind.USER_MESSAGE,
        content={"text": purchase},
    )
    provider = mock.Mock()
    correction = (
        "There is no receipt. I just notified you. Documents are only when I "
        "send photos or actual documents. You should have logged a paid expense "
        "in the ledger."
    )

    with mock.patch("rentium.rama.service.get_provider", return_value=provider):
        preview = run_turn(landlord, correction, conversation_id)
        done = run_turn(landlord, "yes", conversation_id)

    assert "• Amount: $280.00" in preview.reply
    assert "• Bank: paid" in preview.reply
    assert "Logged $280.00" in done.reply
    entries = LedgerEntry.objects.filter(
        landlord=landlord,
        entry_type=EntryType.EXPENSE,
    )
    assert entries.count() == 1
    entry = entries.get()
    assert entry.amount == Decimal("280.00")
    assert entry.paid_on is not None
    assert entry.holding_id == holding.pk
    old_document.refresh_from_db()
    assert old_document.holding_id is None
    assert old_document.ledger_entry_id is None
    assert provider.complete.call_count == 0


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


def test_first_month_discount_stays_on_exact_lease_and_executes_real_plan(
    landlord,
):
    """Regression for two Garden Suite leases plus an old receipt in RAMA.

    The landlord named the pending Naveen/Aishwarya lease. The preview must
    remain bound to that lease, and the next yes must execute that exact plan
    without switching to the newer active lease or reopening a document.
    """
    import uuid
    from datetime import date

    from rentium.leases.models import RentAdjustment
    from rentium.ledger.billing import compute_joint_rent_for_due_date
    from rentium.rama.conversations import record_visible_message
    from rentium.rama.models import RamaDocument
    from rentium.rama.models import RamaMessage
    from rentium.rama.service import _pending_unscoped_document_id
    from rentium.rama.service import _first_month_rent_adjustment_intent
    from rentium.rama.service import run_turn

    _enable_rama(landlord)
    garden = Property.objects.create(
        landlord=landlord,
        name="Garden Suite",
        address="950 McKenzie Ave",
        city="Victoria",
        province="bc",
        property_category=Property.PropertyCategory.ROOM,
        room_type=Property.RoomType.PRIVATE,
    )
    target = Lease.objects.create(
        landlord=landlord,
        property=garden,
        lease_number="RMT652523-C281",
        lease_type=Lease.LeaseType.GENERIC_ROOMMATE,
        status=Lease.LeaseStatus.PENDING_SIGNATURES,
        start_date=date(2026, 8, 1),
        end_date=date(2027, 5, 31),
        total_rent=Decimal("2000.00"),
        security_deposit=Decimal("1000.00"),
    )
    LeaseTenant.objects.create(
        lease=target,
        invited_name="Aishwarya Chenthamara",
        invited_email="aishwarya@example.com",
        rent_amount=Decimal("0.00"),
    )
    LeaseTenant.objects.create(
        lease=target,
        invited_name="Naveen Prasanth Singaravel",
        invited_email="naveen@example.com",
        rent_amount=Decimal("2000.00"),
        is_primary_tenant=True,
    )
    other = Lease.objects.create(
        landlord=landlord,
        property=garden,
        lease_number="RMT698948-2EA3",
        lease_type=Lease.LeaseType.GENERIC_ROOMMATE,
        status=Lease.LeaseStatus.ACTIVE,
        start_date=date(2026, 8, 4),
        end_date=date(2026, 8, 31),
        total_rent=Decimal("1900.00"),
        security_deposit=Decimal("950.00"),
    )
    LeaseTenant.objects.create(
        lease=other,
        invited_name="Gurpreet Singh",
        invited_email="gurpreet@example.com",
        rent_amount=Decimal("1900.00"),
        is_primary_tenant=True,
        has_signed=True,
    )

    # An unscoped document can exist in the portfolio inbox, but it was not
    # introduced in this chat episode and therefore is not conversation state.
    RamaDocument.objects.create(
        landlord=landlord,
        original_file="business_documents/inbox/old-receipt.pdf",
        original_filename="old-receipt.pdf",
        sha256="f" * 64,
        status=RamaDocument.Status.READY,
        amount=Decimal("2.00"),
    )
    conversation_id = uuid.uuid4()
    record_visible_message(
        landlord=landlord,
        conversation_id=conversation_id,
        direction=RamaMessage.Direction.INBOUND,
        text=(
            "the lease with Naveen and Aishwarya - lease "
            f"{target.lease_number}"
        ),
        channel="telegram",
    )
    assert _pending_unscoped_document_id(
        landlord, conversation_id
    ) == ""
    named_scope = _first_month_rent_adjustment_intent(
        landlord,
        conversation_id,
        "adjust Aishwarya's first month rent to be $400 only",
    )
    assert named_scope is not None
    assert "household" in named_scope["error"]
    assert target.lease_number in named_scope["error"]
    prorated_override = _first_month_rent_adjustment_intent(
        landlord,
        conversation_id,
        (
            f'For lease "{other.lease_number}", change the prorated rent that '
            "was set at $1,716.13 to $1900 instead"
        ),
    )
    assert prorated_override == {
        "tool": "apply_rent_adjustment",
        "arguments": {
            "lease_number": other.lease_number,
            "effective_date": "2026-08-04",
            "is_recurring": "0",
            "reason": "One-time first-month rent adjustment",
            "target_lease_total": "1900",
        },
    }

    provider = mock.Mock()
    with mock.patch("rentium.rama.service.get_provider", return_value=provider):
        preview = run_turn(
            landlord,
            "can we discount their first month rent to $400?",
            conversation_id,
            role="general",
            channel="telegram",
        )

    assert preview.deterministic is True
    assert provider.complete.call_count == 0
    assert target.lease_number in preview.reply
    assert "$2000.00" in preview.reply
    assert "$400.00" in preview.reply
    assert "$1600.00" in preview.reply
    assert "2026-08-31" in preview.reply
    assert "document" not in preview.reply.casefold()
    plan = RamaPendingPlan.objects.get(conversation_id=conversation_id)
    step = plan.steps.get()
    assert step.tool == "apply_rent_adjustment"
    assert step.arguments["lease_number"] == target.lease_number
    assert step.arguments["target_lease_total"] == "400.00"
    assert step.arguments["expected_current_total"] == "2000.00"
    assert step.arguments["end_date"] == "2026-08-31"
    assert not RentAdjustment.objects.exists()

    with mock.patch("rentium.rama.service.get_provider", return_value=provider):
        applied = run_turn(
            landlord,
            "yes",
            conversation_id,
            role="general",
            channel="telegram",
        )

    assert applied.deterministic is True
    assert target.lease_number in applied.reply
    assert "Applied one-time discount of 1600.00" in applied.reply
    assert RentAdjustment.objects.filter(
        lease_tenant__lease=target,
        amount=Decimal("1600.00"),
        effective_date=date(2026, 8, 1),
        end_date=date(2026, 8, 31),
    ).count() == 1
    assert not RentAdjustment.objects.filter(lease_tenant__lease=other).exists()
    assert compute_joint_rent_for_due_date(
        target, date(2026, 8, 1)
    ) == Decimal("400.00")
    assert compute_joint_rent_for_due_date(
        target, date(2026, 9, 1)
    ) == Decimal("2000.00")


def test_named_tenant_rent_request_resolves_lease_and_never_edits_terms(
    landlord, bc_lease,
):
    import uuid

    from rentium.leases.models import RentAdjustment
    from rentium.rama.service import run_turn

    _enable_rama(landlord)
    bc_lease.special_terms = "Keep the original legal clause."
    bc_lease.save(update_fields=["special_terms"])
    LeaseTenant.objects.create(
        lease=bc_lease,
        invited_name="Aishwarya Chenthamara",
        invited_email="aishwarya@example.com",
        rent_amount=Decimal("0.00"),
    )
    LeaseTenant.objects.create(
        lease=bc_lease,
        invited_name="Naveen Prasanth Singaravel",
        invited_email="naveen@example.com",
        rent_amount=bc_lease.total_rent,
        is_primary_tenant=True,
    )
    conversation_id = uuid.uuid4()
    provider = mock.Mock()

    with mock.patch("rentium.rama.service.get_provider", return_value=provider):
        scoped = run_turn(
            landlord,
            "can u adjust Aishwarya's first month rent to be $400 only?",
            conversation_id,
            role="general",
        )

    assert scoped.deterministic is True
    assert provider.complete.call_count == 0
    assert bc_lease.lease_number in scoped.reply
    assert "household" in scoped.reply
    assert "Aishwarya" in scoped.reply
    assert not RamaPendingPlan.objects.filter(
        conversation_id=conversation_id,
    ).exists()

    with mock.patch("rentium.rama.service.get_provider", return_value=provider):
        preview = run_turn(
            landlord,
            "the entire household's first month total $400",
            conversation_id,
            role="general",
        )

    assert preview.deterministic is True
    assert provider.complete.call_count == 0
    plan = RamaPendingPlan.objects.get(conversation_id=conversation_id)
    step = plan.steps.get()
    assert step.tool == "apply_rent_adjustment"
    assert step.arguments["target_lease_total"] == "400.00"
    assert "special_terms" not in step.arguments

    with mock.patch("rentium.rama.service.get_provider", return_value=provider):
        done = run_turn(landlord, "yes", conversation_id, role="general")

    bc_lease.refresh_from_db()
    assert done.deterministic is True
    assert bc_lease.special_terms == "Keep the original legal clause."
    adjustment = RentAdjustment.objects.get(lease_tenant__lease=bc_lease)
    assert adjustment.amount == bc_lease.total_rent - Decimal("400.00")


def test_two_named_august_rent_targets_compile_to_one_confirmable_batch(landlord):
    import uuid
    from datetime import date

    from rentium.leases.models import RentAdjustment
    from rentium.ledger.billing import compute_joint_rent_for_due_date
    from rentium.rama.service import run_turn

    _enable_rama(landlord)
    garden = _room(landlord, "Garden Suite", "950 McKenzie Ave")
    aishwarya_lease = Lease.objects.create(
        landlord=landlord,
        property=garden,
        lease_number="RMT652523-C281",
        lease_type=Lease.LeaseType.GENERIC_ROOMMATE,
        status=Lease.LeaseStatus.PENDING_SIGNATURES,
        start_date=date(2026, 8, 1),
        move_in_date=date(2026, 9, 1),
        end_date=date(2027, 5, 31),
        total_rent=Decimal("2000.00"),
    )
    LeaseTenant.objects.create(
        lease=aishwarya_lease,
        invited_name="Aishwarya Chenthamara",
        invited_email="aishwarya@example.com",
        rent_amount=Decimal("0.00"),
    )
    LeaseTenant.objects.create(
        lease=aishwarya_lease,
        invited_name="Naveen Prasanth Singaravel",
        invited_email="naveen@example.com",
        rent_amount=Decimal("2000.00"),
        is_primary_tenant=True,
    )
    gurpreet_lease = Lease.objects.create(
        landlord=landlord,
        property=garden,
        lease_number="RMT698948-2EA3",
        lease_type=Lease.LeaseType.GENERIC_ROOMMATE,
        status=Lease.LeaseStatus.ACTIVE,
        start_date=date(2026, 8, 4),
        end_date=date(2026, 8, 31),
        total_rent=Decimal("1900.00"),
    )
    gurpreet = LeaseTenant.objects.create(
        lease=gurpreet_lease,
        invited_name="Gurpreet Singh",
        invited_email="gurpreet@example.com",
        rent_amount=Decimal("1900.00"),
        is_primary_tenant=True,
        has_signed=True,
    )
    RentAdjustment.create_proration(
        gurpreet,
        date(2026, 8, 4),
        date(2026, 8, 31),
        landlord,
    )
    conversation_id = uuid.uuid4()
    provider = mock.Mock()

    with mock.patch("rentium.rama.service.get_provider", return_value=provider):
        preview = run_turn(
            landlord,
            (
                "like make aug rent $400 for aishwarya and also make aug rent "
                "$1900 for gurpreet singh"
            ),
            conversation_id,
            role="general",
            channel="telegram",
        )

    assert preview.deterministic is True
    assert provider.complete.call_count == 0
    assert "all 2 changes" in preview.reply
    assert "RMT652523-C281" in preview.reply
    assert "RMT698948-2EA3" in preview.reply
    plan = RamaPendingPlan.objects.get(conversation_id=conversation_id)
    assert plan.steps.count() == 2
    assert (plan.task.input or {})["intent_contract"]["version"] == 2

    with mock.patch("rentium.rama.service.get_provider", return_value=provider):
        applied = run_turn(
            landlord,
            "yes",
            conversation_id,
            role="general",
            channel="telegram",
        )

    assert applied.deterministic is True
    assert provider.complete.call_count == 0
    assert "Applied" in applied.reply, applied.reply
    assert compute_joint_rent_for_due_date(
        aishwarya_lease, date(2026, 8, 1)
    ) == Decimal("400.00")
    assert compute_joint_rent_for_due_date(
        aishwarya_lease, date(2026, 9, 1)
    ) == Decimal("2000.00")
    assert compute_joint_rent_for_due_date(
        gurpreet_lease, date(2026, 8, 4)
    ) == Decimal("1900.00")


def test_singular_gurpreet_august_target_prices_posted_proration_and_followup(
    landlord,
):
    import uuid
    from datetime import date

    from rentium.leases.models import RentAdjustment
    from rentium.ledger.billing import ensure_joint_rent_charge
    from rentium.ledger.models import EntryType
    from rentium.ledger.models import LedgerEntry
    from rentium.rama.plan_runner import clear_plan
    from rentium.rama.service import run_turn

    _enable_rama(landlord)
    garden = _room(landlord, "Garden Suite", "950 McKenzie Ave")
    lease = Lease.objects.create(
        landlord=landlord,
        property=garden,
        lease_number="RMT698948-2EA3",
        lease_type=Lease.LeaseType.GENERIC_ROOMMATE,
        status=Lease.LeaseStatus.ACTIVE,
        start_date=date(2026, 8, 4),
        move_in_date=date(2026, 8, 4),
        end_date=date(2026, 8, 31),
        total_rent=Decimal("1900.00"),
    )
    gurpreet = LeaseTenant.objects.create(
        lease=lease,
        invited_name="Gurpreet Singh",
        invited_email="gurpreet@example.com",
        rent_amount=Decimal("1900.00"),
        is_primary_tenant=True,
        has_signed=True,
    )
    RentAdjustment.create_proration(
        gurpreet,
        date(2026, 8, 4),
        date(2026, 8, 31),
        landlord,
    )
    charge, created = ensure_joint_rent_charge(lease, date(2026, 8, 4))
    assert created is True
    assert charge.amount == Decimal("1716.13")

    conversation_id = uuid.uuid4()
    provider = mock.Mock()
    with mock.patch("rentium.rama.service.get_provider", return_value=provider):
        preview = run_turn(
            landlord,
            "hey lso make aug rent $1900 for gurpreet singh",
            conversation_id,
            role="general",
            channel="telegram",
        )

    assert preview.deterministic is True
    assert provider.complete.call_count == 0
    plan = RamaPendingPlan.objects.get(conversation_id=conversation_id)
    step = plan.steps.get()
    assert step.arguments["lease_number"] == lease.lease_number
    assert step.arguments["effective_date"] == "2026-08-04"
    assert step.arguments["expected_current_total"] == "1716.13"
    assert step.arguments["target_lease_total"] == "1900.00"

    # Reproduce the terse correction after a prior attempt returned no plan.
    clear_plan(landlord, conversation_id)
    with mock.patch("rentium.rama.service.get_provider", return_value=provider):
        followup = run_turn(
            landlord,
            "yes but the ledger has prorated rate, make it 1900 man",
            conversation_id,
            role="general",
            channel="telegram",
        )

    assert followup.deterministic is True
    assert provider.complete.call_count == 0
    followup_step = RamaPendingPlan.objects.get(
        conversation_id=conversation_id,
    ).steps.get()
    assert followup_step.arguments["effective_date"] == "2026-08-04"
    assert followup_step.arguments["expected_current_total"] == "1716.13"

    with mock.patch("rentium.rama.service.get_provider", return_value=provider):
        applied = run_turn(
            landlord,
            "yes",
            conversation_id,
            role="general",
            channel="telegram",
        )

    assert applied.deterministic is True
    live_charge = LedgerEntry.objects.not_voided().get(
        lease=lease,
        entry_type=EntryType.RENT_CHARGE,
        due_date=date(2026, 8, 4),
    )
    assert live_charge.amount == Decimal("1900.00")


def test_explicit_august_target_adds_pre_move_in_charge_period(landlord):
    from datetime import date

    from rentium.leases.models import RentAdjustment
    from rentium.ledger.billing import _lease_rent_due_dates
    from rentium.ledger.billing import ensure_joint_rent_charge

    garden = _room(landlord, "Garden Suite", "950 McKenzie Ave")
    lease = Lease.objects.create(
        landlord=landlord,
        property=garden,
        lease_number="RMT652523-C281",
        lease_type=Lease.LeaseType.GENERIC_ROOMMATE,
        status=Lease.LeaseStatus.ACTIVE,
        start_date=date(2026, 8, 1),
        move_in_date=date(2026, 9, 1),
        end_date=date(2027, 5, 31),
        total_rent=Decimal("2000.00"),
    )
    slot = LeaseTenant.objects.create(
        lease=lease,
        invited_name="Naveen Prasanth Singaravel",
        invited_email="naveen@example.com",
        rent_amount=Decimal("2000.00"),
        is_primary_tenant=True,
        has_signed=True,
    )
    RentAdjustment.objects.create(
        lease_tenant=slot,
        adjustment_type=RentAdjustment.AdjustmentType.DISCOUNT,
        calculation_method=RentAdjustment.CalculationMethod.FLAT_AMOUNT,
        amount=Decimal("1600.00"),
        target_amount=Decimal("400.00"),
        reason="Explicit August household rent target",
        effective_date=date(2026, 8, 1),
        end_date=date(2026, 8, 31),
        is_recurring=False,
        created_by=landlord,
    )

    due_dates = list(_lease_rent_due_dates(lease, date(2026, 9, 30)))
    assert due_dates == [date(2026, 8, 1), date(2026, 9, 1)]
    august, _ = ensure_joint_rent_charge(lease, due_dates[0])
    september, _ = ensure_joint_rent_charge(lease, due_dates[1])
    assert august.amount == Decimal("400.00")
    assert september.amount == Decimal("2000.00")


def test_august_rent_can_be_target_bypasses_unavailable_provider(landlord):
    import uuid
    from datetime import date

    from rentium.rama.service import run_turn

    _enable_rama(landlord)
    garden = _room(landlord, "Garden Suite", "950 McKenzie Ave")
    lease = Lease.objects.create(
        landlord=landlord,
        property=garden,
        lease_number="RMT652523-C281",
        lease_type=Lease.LeaseType.GENERIC_ROOMMATE,
        status=Lease.LeaseStatus.PENDING_SIGNATURES,
        start_date=date(2026, 8, 1),
        move_in_date=date(2026, 9, 1),
        end_date=date(2027, 5, 31),
        total_rent=Decimal("2000.00"),
    )
    LeaseTenant.objects.create(
        lease=lease,
        invited_name="Aishwarya Chenthamara",
        invited_email="aishwarya@example.com",
        rent_amount=Decimal("0.00"),
    )
    LeaseTenant.objects.create(
        lease=lease,
        invited_name="Naveen Prasanth Singaravel",
        invited_email="naveen@example.com",
        rent_amount=Decimal("2000.00"),
        is_primary_tenant=True,
    )
    provider = mock.Mock()
    provider.complete.side_effect = RuntimeError("OpenAI unavailable")
    conversation_id = uuid.uuid4()

    with mock.patch("rentium.rama.service.get_provider", return_value=provider):
        preview = run_turn(
            landlord,
            "ok and aug rent for aishwarya and naveen can be $400 ?",
            conversation_id,
            role="general",
            channel="telegram",
        )

    assert preview.deterministic is True
    assert provider.complete.call_count == 0
    plan = RamaPendingPlan.objects.get(conversation_id=conversation_id)
    step = plan.steps.get()
    assert step.arguments["lease_number"] == lease.lease_number
    assert step.arguments["effective_date"] == "2026-08-01"
    assert step.arguments["expected_current_total"] == "2000.00"
    assert step.arguments["target_lease_total"] == "400.00"


def test_overlapping_followup_gets_an_immediate_deterministic_reply(landlord):
    import uuid
    from contextlib import contextmanager

    from rentium.rama.service import run_turn

    @contextmanager
    def busy_guard(*_args, **_kwargs):
        yield False

    with mock.patch(
        "rentium.rama.turn_guard.conversation_turn_guard",
        busy_guard,
    ):
        result = run_turn(landlord, "?", uuid.uuid4(), role="general")

    assert result.deterministic is True
    assert result.model == "turn-guard"
    assert "still finishing your previous message" in result.reply


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
            "cleaning_deposit": "0",
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
            "cleaning_deposit": "0.00",
        },
        format="json",
    )
    assert response.status_code == 201, response.data
    api_lease = Lease.objects.get(pk=response.data["id"])
    assert rama_lease.common_space_shared_with == ["LANDLORD"]
    assert api_lease.common_space_shared_with == ["LANDLORD"]


# ===================== "can u add a $200 cleaning deposit to lease X"
# 2026-08-02. Asked to add a cleaning deposit to lease RMT415536-0617, RAMA
# said it had no tool for it, then that the lease was "already signed and
# active, so its deposit fields are locked". The lease was PENDING with one
# tenant signed and the landlord not — is_locked() was False the whole time.
#
# The refusal was honest about the menu it was given: retrieval offered
# create_lease, adjust_lease and mark_cleaning_deposit_paid and never offered
# update_lease. The invented lock rule was what the model reached for to
# explain a gap it couldn't see the shape of. Pinning the tool removes both:
# the tool's own guard answers the lock question from the record.
def test_setting_a_deposit_on_an_existing_lease_reaches_update_lease():
    from rentium.rama.capabilities import (
        select_tool_schemas,
        supported_tool_for_request,
    )
    from rentium.rama.registry import tool_schemas

    asks = [
        "can u add a $200 cleaning deposit to lease RMT415536-0617",
        "yes, edit the lease deposit field",
        "set the security deposit to 500 on lease RMT415536-0617",
        "change the cleaning deposit to 350",
    ]
    for ask in asks:
        assert supported_tool_for_request(ask) == "update_lease", ask
        offered = [s["name"] for s in select_tool_schemas(ask, tool_schemas(), limit=12)]
        assert "update_lease" in offered, ask


@pytest.mark.parametrize(
    "ask,expected",
    [
        # The more specific things to do with a deposit keep their routing.
        ("cleaning deposit paid", "mark_cleaning_deposit_paid"),
        ("return the deposit", "return_deposits"),
        ("deduct 80 from the deposit for garbage removal", "record_deposit_deduction"),
        # And creating a lease WITH a deposit is still creating a lease.
        ("create a lease for Room C with a $500 deposit", "create_lease"),
    ],
)
def test_the_deposit_edit_route_does_not_swallow_its_neighbours(ask, expected):
    from rentium.rama.capabilities import supported_tool_for_request

    assert supported_tool_for_request(ask) == expected


def test_a_pending_lease_with_one_signature_is_editable(landlord):
    """The fact RAMA got wrong. A lease is locked at ACTIVE, not at the first
    signature — so 'already signed, therefore locked' is never a valid reason
    to refuse on a PENDING lease."""
    from datetime import date

    prop = Property.objects.create(
        landlord=landlord,
        name="Signed-Once Suite",
        address="9 Partial St",
        city="Victoria",
        province="bc",
        property_category=Property.PropertyCategory.ROOM,
        room_type=Property.RoomType.PRIVATE,
    )
    lease = Lease.objects.create(
        landlord=landlord,
        property=prop,
        lease_type=Lease.LeaseType.GENERIC_ROOMMATE,
        status=Lease.LeaseStatus.PENDING_SIGNATURES,
        start_date=date(2026, 9, 1),
        end_date=date(2027, 8, 31),
        total_rent=Decimal("900.00"),
    )
    LeaseTenant.objects.create(
        lease=lease,
        invited_email="one@example.com",
        invited_name="One Signer",
        rent_amount=Decimal("900.00"),
        is_primary_tenant=True,
        has_signed=True,
    )
    assert lease.is_locked() is False

    result = registry.execute(
        "update_lease",
        {
            "lease_number": lease.lease_number,
            "cleaning_deposit": "200",
            "confirm": "yes",
        },
        landlord=landlord,
    )
    assert result.get("updated") is True, result
    lease.refresh_from_db()
    assert lease.cleaning_deposit == Decimal("200.00")
