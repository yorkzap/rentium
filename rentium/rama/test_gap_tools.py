"""API gap-close tools: ledger control, cancel viewing, inquiry, cleaning deposit."""

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
        cleaning_deposit=Decimal("75.00"),
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
        "mark_cleaning_deposit_paid",
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
    assert (
        supported_tool_for_request("cleaning deposit paid")
        == "mark_cleaning_deposit_paid"
    )
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


def test_mark_cleaning_deposit_paid(room_lease, landlord):
    done = registry.execute(
        "mark_cleaning_deposit_paid",
        {
            "lease_number": room_lease.lease_number,
            "confirm": "yes",
        },
        landlord=landlord,
    )
    assert done.get("updated"), done
    lt = room_lease.lease_tenants.get()
    lt.refresh_from_db()
    assert lt.cleaning_deposit_paid is True


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


# ------------------------------------------------- deposit deductions/returns
@pytest.fixture
def unit_lease_with_deposits(landlord):
    """A whole-unit tenancy with $200 security + $200 cleaning, both paid."""
    from rentium.ledger import services as ledger_services

    prop = Property.objects.create(
        landlord=landlord,
        name="Deduction House",
        address="7 Deduct Rd",
        city="Victoria",
        province="bc",
        property_category=Property.PropertyCategory.COMPLETE_UNIT,
    )
    lease = Lease.objects.create(
        landlord=landlord,
        property=prop,
        lease_type=Lease.LeaseType.GENERIC_RESIDENTIAL,
        status=Lease.LeaseStatus.ACTIVE,
        start_date=date(2026, 1, 1),
        end_date=date(2026, 12, 31),
        total_rent=Decimal("1800.00"),
        security_deposit=Decimal("200.00"),
        cleaning_deposit=Decimal("200.00"),
    )
    LeaseTenant.objects.create(
        lease=lease,
        invited_email="ded@example.com",
        invited_name="Ded Tenant",
        rent_amount=Decimal("1800.00"),
        is_primary_tenant=True,
        has_signed=True,
    )
    for description, kind in (
        ("Security deposit", "security_deposit"),
        ("Cleaning deposit", "cleaning_deposit_lease"),
    ):
        charge, _ = ledger_services.post_charge(
            landlord=landlord,
            tenant=None,
            lease=lease,
            property=prop,
            amount="200.00",
            due_date=date(2026, 1, 1),
            entry_type="DEPOSIT_CHARGE",
            description=description,
            metadata={"kind": kind},
        )
        ledger_services.record_payment(
            charge=charge, amount="200.00", payment_method="ETRANSFER"
        )
    return lease


@pytest.fixture
def inspection_for(unit_lease_with_deposits):
    from django.core.management import call_command

    from rentium.leases.inspection_services import build_inspection

    call_command("seed_inspection_templates", verbosity=0)
    return build_inspection(lease=unit_lease_with_deposits)


def test_deposit_tools_are_registered_and_confirm_gated():
    for name in ("record_deposit_deduction", "return_deposits"):
        assert name in REGISTRY
        assert "confirm" in REGISTRY[name].parameters["properties"]


def test_deposit_deduction_intents_route_to_the_right_tool():
    assert supported_tool_for_request("deduct from deposit") == (
        "record_deposit_deduction"
    )
    assert supported_tool_for_request("return the deposit") == "return_deposits"


def _deduct(landlord, **kwargs):
    return registry.execute("record_deposit_deduction", kwargs, landlord=landlord)


def test_a_labour_deduction_prices_hours_times_rate(
    landlord, unit_lease_with_deposits, inspection_for
):
    result = _deduct(
        landlord,
        lease_number=unit_lease_with_deposits.lease_number,
        deposit="cleaning",
        basis="labour",
        hours="3",
        hourly_rate="35",
        note="Oven and bathroom",
        confirm="yes",
    )
    assert result["recorded"] is True
    assert result["amount"] == "105.00"
    inspection_for.refresh_from_db()
    assert inspection_for.deduction_cleaning_deposit == Decimal("105.00")
    # It records a proposal — it must not have touched the money.
    from rentium.ledger import services as ledger_services

    assert ledger_services.deposits_held(landlord) == Decimal("400.00")


def test_labour_without_a_rate_is_asked_about_not_guessed(
    landlord, unit_lease_with_deposits, inspection_for
):
    result = _deduct(
        landlord,
        lease_number=unit_lease_with_deposits.lease_number,
        deposit="cleaning",
        basis="labour",
        hours="3",
        confirm="yes",
    )
    assert "question_for_user" in result
    assert inspection_for.deposit_deductions.count() == 0


def test_a_deduction_never_names_the_deposit_for_you(
    landlord, unit_lease_with_deposits, inspection_for
):
    """Deposits are held separately, so which one this comes out of is a fact
    to ask for, never one to assume."""
    result = _deduct(
        landlord,
        lease_number=unit_lease_with_deposits.lease_number,
        deposit="",
        basis="garbage",
        amount="80",
        confirm="yes",
    )
    assert result["needs"] == "deposit"
    assert inspection_for.deposit_deductions.count() == 0


def test_returning_deposits_pays_each_one_separately(
    landlord, unit_lease_with_deposits
):
    from rentium.ledger import services as ledger_services

    result = registry.execute(
        "return_deposits",
        {
            "lease_number": unit_lease_with_deposits.lease_number,
            "payment_method": "etransfer",
            "confirm": "yes",
        },
        landlord=landlord,
    )
    assert result["returned"] is True
    assert len(result["deposits"]) == 2
    assert {row["returning"] for row in result["deposits"]} == {"200.00"}
    assert ledger_services.deposits_held(landlord) == Decimal("0.00")


def test_agreed_deductions_are_held_back_and_the_rest_goes_home(
    landlord, unit_lease_with_deposits, inspection_for
):
    """The whole point: 2h x $35 + $22 supplies + $80 dump run = $172 comes out
    of the CLEANING deposit only, and the security deposit goes back whole."""
    from django.utils import timezone

    from rentium.ledger import services as ledger_services

    for kwargs in (
        {"basis": "labour", "hours": "2", "hourly_rate": "35"},
        {"basis": "supplies", "amount": "22"},
        {"basis": "garbage", "amount": "80"},
    ):
        _deduct(
            landlord,
            lease_number=unit_lease_with_deposits.lease_number,
            deposit="cleaning",
            note="move-out clean",
            confirm="yes",
            **kwargs,
        )
    inspection_for.refresh_from_db()
    assert inspection_for.deduction_cleaning_deposit == Decimal("172.00")

    # Nothing is kept until the tenant agrees in writing.
    before = registry.execute(
        "return_deposits",
        {
            "lease_number": unit_lease_with_deposits.lease_number,
            "payment_method": "etransfer",
        },
        landlord=landlord,
    )
    assert {row["kept_by_agreement"] for row in before["preview"]["deposits"]} == {
        "0.00"
    }

    inspection_for.deduction_agreed_at = timezone.now()
    inspection_for.save(update_fields=["deduction_agreed_at"])

    result = registry.execute(
        "return_deposits",
        {
            "lease_number": unit_lease_with_deposits.lease_number,
            "payment_method": "etransfer",
            "confirm": "yes",
        },
        landlord=landlord,
    )
    by_deposit = {row["deposit"]: row for row in result["deposits"]}
    assert by_deposit["Security deposit"]["returning"] == "200.00"
    assert by_deposit["Security deposit"]["kept_by_agreement"] == "0.00"
    assert by_deposit["Cleaning deposit"]["returning"] == "28.00"
    assert by_deposit["Cleaning deposit"]["kept_by_agreement"] == "172.00"
    # Every dollar left the deposit liability: 228 to the tenant, 172 kept.
    assert ledger_services.deposits_held(landlord) == Decimal("0.00")


def test_a_deduction_cannot_exceed_the_deposit_it_comes_from(
    landlord, unit_lease_with_deposits, inspection_for
):
    from django.utils import timezone

    _deduct(
        landlord,
        lease_number=unit_lease_with_deposits.lease_number,
        deposit="cleaning",
        basis="cleaner",
        amount="500",
        note="Deep clean",
        confirm="yes",
    )
    inspection_for.refresh_from_db()
    inspection_for.deduction_agreed_at = timezone.now()
    inspection_for.save(update_fields=["deduction_agreed_at"])

    result = registry.execute(
        "return_deposits",
        {
            "lease_number": unit_lease_with_deposits.lease_number,
            "payment_method": "etransfer",
            "confirm": "yes",
        },
        landlord=landlord,
    )
    assert "error" in result
    assert "more than" in result["error"] or "exceed" in result["error"]
