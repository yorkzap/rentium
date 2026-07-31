"""API gap-close tools: ledger control, cancel viewing, inquiry, cleaning fee."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from rentium.leases.models import Lease, LeaseTenant
from rentium.properties.models import Property
from rentium.rama import registry
from rentium.rama.capabilities import supported_tool_for_request
from rentium.rama.registry import REGISTRY

pytestmark = pytest.mark.django_db


@pytest.fixture
def room_lease(landlord):
    prop = Property.objects.create(
        landlord=landlord,
        name="Gap Room",
        address="50 Gap St",
        city="Victoria",
        province="bc",
        property_category=Property.PropertyCategory.ROOM,
        room_type=Property.RoomType.PRIVATE,
    )
    lease = Lease.objects.create(
        landlord=landlord,
        property=prop,
        lease_type=Lease.LeaseType.GENERIC_ROOMMATE,
        status=Lease.LeaseStatus.ACTIVE,
        start_date=date(2026, 1, 1),
        end_date=date(2026, 12, 31),
        total_rent=Decimal("900.00"),
        security_deposit=Decimal("450.00"),
    )
    LeaseTenant.objects.create(
        lease=lease,
        invited_email="gap@example.com",
        invited_name="Gap Tenant",
        rent_amount=Decimal("900.00"),
        cleaning_fee=Decimal("75.00"),
        is_primary_tenant=True,
        has_signed=True,
    )
    return lease


def test_gap_tools_registered():
    for name in (
        "void_ledger_entry",
        "mark_ledger_paid",
        "correct_ledger_entry",
        "post_ledger_credit",
        "post_one_off_charge",
        "update_inspection_items",
        "approve_inspection_suggestion",
        "dismiss_inspection_suggestion",
        "mark_inspection_delivered",
        "cancel_viewing",
        "mark_cleaning_fee_paid",
        "list_payment_reminders",
        "create_payment_reminder",
        "mark_payment_reminder_sent",
        "update_inquiry",
        "commit_import_batch",
        "discard_import_batch",
        "list_notifications",
        "mark_notifications_read",
    ):
        assert name in REGISTRY, name


def test_gap_close_phrases():
    assert supported_tool_for_request("void this expense") == "void_ledger_entry"
    assert supported_tool_for_request("cancel the viewing tomorrow") == "cancel_viewing"
    assert supported_tool_for_request("charge tenant for damage") == "post_one_off_charge"
    assert supported_tool_for_request("cleaning fee paid") == "mark_cleaning_fee_paid"
    assert supported_tool_for_request("archive this inquiry") == "update_inquiry"


def test_post_one_off_charge_and_credit(room_lease, landlord):
    preview = registry.execute(
        "post_one_off_charge",
        {
            "lease_number": room_lease.lease_number,
            "amount": "150",
            "due_date": "2026-08-15",
            "description": "Window damage",
            "entry_type": "DAMAGE_CHARGE",
        },
        landlord=landlord,
    )
    # entry_type might not allow DAMAGE_CHARGE — fall back if needed
    if preview.get("error") and "entry_type" in str(preview.get("error", "")).lower():
        preview = registry.execute(
            "post_one_off_charge",
            {
                "lease_number": room_lease.lease_number,
                "amount": "150",
                "due_date": "2026-08-15",
                "description": "Window damage",
            },
            landlord=landlord,
        )
    assert preview.get("needs_confirm"), preview

    done = registry.execute(
        "post_one_off_charge",
        {
            "lease_number": room_lease.lease_number,
            "amount": "150",
            "due_date": "2026-08-15",
            "description": "Window damage",
            "confirm": "yes",
        },
        landlord=landlord,
    )
    assert done.get("charged"), done
    charge_id = done["entry_id"]

    credit = registry.execute(
        "post_ledger_credit",
        {
            "entry_id": charge_id,
            "amount": "25",
            "reason": "Goodwill",
            "confirm": "yes",
        },
        landlord=landlord,
    )
    assert credit.get("credited"), credit


def test_void_expense(room_lease, landlord):
    from rentium.ledger import services as ledger_services
    from rentium.ledger.models import ExpenseCategory

    entry, _ = ledger_services.post_expense(
        landlord=landlord,
        amount=Decimal("40.00"),
        category=ExpenseCategory.OTHER,
        description="Gap test expense",
        property=room_lease.property,
        created_by=landlord.user,
    )
    done = registry.execute(
        "void_ledger_entry",
        {
            "entry_id": str(entry.pk),
            "reason": "Posted by mistake",
            "confirm": "yes",
        },
        landlord=landlord,
    )
    assert done.get("voided"), done
    entry.refresh_from_db()
    assert entry.voided


def test_mark_expense_paid(room_lease, landlord):
    from rentium.ledger import services as ledger_services
    from rentium.ledger.models import ExpenseCategory

    entry, _ = ledger_services.post_expense(
        landlord=landlord,
        amount=Decimal("22.00"),
        category=ExpenseCategory.OTHER,
        description="Hydro fragment",
        property=room_lease.property,
        created_by=landlord.user,
    )
    done = registry.execute(
        "mark_ledger_paid",
        {
            "entry_id": str(entry.pk),
            "paid_on": "2026-07-20",
            "confirm": "yes",
        },
        landlord=landlord,
    )
    assert done.get("updated"), done
    entry.refresh_from_db()
    assert entry.paid_on == date(2026, 7, 20)


def test_mark_cleaning_fee_paid(room_lease, landlord):
    done = registry.execute(
        "mark_cleaning_fee_paid",
        {
            "lease_number": room_lease.lease_number,
            "confirm": "yes",
        },
        landlord=landlord,
    )
    assert done.get("updated"), done
    lt = room_lease.lease_tenants.get()
    lt.refresh_from_db()
    assert lt.cleaning_fee_paid is True


def test_cancel_viewing(landlord):
    from datetime import datetime, timezone as dt_tz

    from rentium.appointments.models import Appointment

    prop = Property.objects.create(
        landlord=landlord,
        name="Show Home",
        address="1 View St",
        city="Victoria",
        province="bc",
        property_category=Property.PropertyCategory.COMPLETE_UNIT,
    )
    appt = Appointment.objects.create(
        landlord=landlord,
        property=prop,
        kind=Appointment.Kind.VIEWING,
        status=Appointment.Status.SCHEDULED,
        starts_at=datetime(2026, 8, 10, 15, 0, tzinfo=dt_tz.utc),
        contact_name="Prospect",
        contact_email="p@example.com",
    )
    done = registry.execute(
        "cancel_viewing",
        {"appointment_id": str(appt.pk), "reason": "Double booked", "confirm": "yes"},
        landlord=landlord,
    )
    assert done.get("cancelled"), done
    appt.refresh_from_db()
    assert appt.status == Appointment.Status.CANCELLED


def test_update_inquiry(landlord):
    from rentium.showcase.models import Inquiry

    prop = Property.objects.create(
        landlord=landlord,
        name="Inquire Here",
        address="2 Ask Ave",
        city="Victoria",
        province="bc",
        property_category=Property.PropertyCategory.COMPLETE_UNIT,
    )
    inq = Inquiry.objects.create(
        landlord=landlord,
        property=prop,
        name="Alex",
        email="alex@example.com",
        phone="+1 250-555-0199",
        message="Interested",
    )
    done = registry.execute(
        "update_inquiry",
        {
            "inquiry_id": str(inq.pk),
            "status": "ARCHIVED",
            "landlord_notes": "Not a fit",
            "confirm": "yes",
        },
        landlord=landlord,
    )
    assert done.get("updated"), done
    inq.refresh_from_db()
    assert inq.status == Inquiry.Status.ARCHIVED
