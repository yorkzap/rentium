"""Phase 1 composites: renew, move-out, inspection package, rent adjust,
utility bill, inquiry→viewing."""

from __future__ import annotations

from datetime import date
from datetime import timedelta
from decimal import Decimal

import pytest

from rentium.leases.models import Lease, LeaseTenant, RentAdjustment
from rentium.ledger.billing import compute_joint_rent_for_due_date
from rentium.ledger.billing import ensure_joint_rent_charge
from rentium.ledger.models import EntryType
from rentium.ledger.models import LedgerEntry
from rentium.properties.models import Property
from rentium.rama import registry
from rentium.rama.capabilities import supported_tool_for_request
from rentium.rama.registry import REGISTRY

pytestmark = pytest.mark.django_db


@pytest.fixture
def active_lease(landlord):
    prop = Property.objects.create(
        landlord=landlord,
        name="Room Composite",
        address="100 Test St",
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
        is_month_to_month=False,
        total_rent=Decimal("1000.00"),
        security_deposit=Decimal("500.00"),
    )
    LeaseTenant.objects.create(
        lease=lease,
        invited_email="tenant@example.com",
        invited_name="Test Tenant",
        rent_amount=Decimal("1000.00"),
        is_primary_tenant=True,
        has_signed=True,
    )
    return lease


def test_composites_registered():
    for name in (
        "renew_lease",
        "settle_moveout",
        "complete_inspection_package",
        "apply_rent_adjustment",
        "record_utility_bill",
        "convert_inquiry_to_viewing",
    ):
        assert name in REGISTRY, name
        assert "confirm" in REGISTRY[name].parameters["properties"]


def test_capability_gap_close_phrases():
    assert supported_tool_for_request("renew the lease for another year") == "renew_lease"
    assert supported_tool_for_request("settle the security deposit return") == "settle_moveout"
    assert (
        supported_tool_for_request("do the condition inspection package")
        == "complete_inspection_package"
    )
    assert supported_tool_for_request("give a $50 rent discount") == "apply_rent_adjustment"
    assert supported_tool_for_request("record the hydro utility bill") == "record_utility_bill"
    assert (
        supported_tool_for_request("turn this inquiry into a viewing")
        == "convert_inquiry_to_viewing"
    )


def test_renew_lease_preview_and_commit(active_lease, landlord):
    preview = registry.execute(
        "renew_lease",
        {
            "lease_number": active_lease.lease_number,
            "start_date": "2027-01-01",
            "end_date": "2027-12-31",
            "total_rent": "1050",
        },
        landlord=landlord,
    )
    assert preview.get("needs_confirm"), preview
    assert preview["preview"]["old_becomes"] == Lease.LeaseStatus.RENEWED

    done = registry.execute(
        "renew_lease",
        {
            "lease_number": active_lease.lease_number,
            "start_date": "2027-01-01",
            "end_date": "2027-12-31",
            "total_rent": "1050",
            "confirm": "yes",
        },
        landlord=landlord,
    )
    assert done.get("renewed"), done
    active_lease.refresh_from_db()
    assert active_lease.status == Lease.LeaseStatus.RENEWED
    new = Lease.objects.get(pk=done["new_lease"]["id"])
    assert new.status == Lease.LeaseStatus.DRAFT
    assert new.previous_lease_id == active_lease.pk
    assert new.total_rent == Decimal("1050.00")
    assert new.start_date == date(2027, 1, 1)
    assert new.lease_tenants.count() == 1


def test_settle_moveout_mutual_agreement(active_lease, landlord):
    end = (date.today() + timedelta(days=14)).isoformat()
    preview = registry.execute(
        "settle_moveout",
        {
            "lease_number": active_lease.lease_number,
            "requested_end_date": end,
            "kind": "MUTUAL_AGREEMENT",
            "reason": "Tenant relocating",
        },
        landlord=landlord,
    )
    assert preview.get("needs_confirm"), preview

    done = registry.execute(
        "settle_moveout",
        {
            "lease_number": active_lease.lease_number,
            "requested_end_date": end,
            "kind": "MUTUAL_AGREEMENT",
            "reason": "Tenant relocating",
            "confirm": "yes",
        },
        landlord=landlord,
    )
    assert done.get("settled"), done
    assert done.get("status") == "PENDING"
    mid = done["moveout_id"]

    settled = registry.execute(
        "settle_moveout",
        {
            "moveout_id": mid,
            "forwarding_address": "99 Next St, Victoria BC",
            "forwarding_address_received_on": date.today().isoformat(),
            "deposit_settlement": "RETURNED",
            "confirm": "yes",
        },
        landlord=landlord,
    )
    assert settled.get("settled"), settled
    assert settled.get("deposit_settlement") == "RETURNED"


def test_apply_rent_adjustment(active_lease, landlord):
    preview = registry.execute(
        "apply_rent_adjustment",
        {
            "lease_number": active_lease.lease_number,
            "adjustment_type": "DISCOUNT",
            "amount": "50",
            "reason": "Good tenant",
            "effective_date": "2026-08-01",
        },
        landlord=landlord,
    )
    assert preview.get("needs_confirm"), preview
    assert preview["preview"]["end_date"] == "2026-08-31"

    done = registry.execute(
        "apply_rent_adjustment",
        {
            "lease_number": active_lease.lease_number,
            "adjustment_type": "DISCOUNT",
            "amount": "50",
            "reason": "Good tenant",
            "effective_date": "2026-08-01",
            "confirm": "yes",
        },
        landlord=landlord,
    )
    assert done.get("applied"), done
    assert RentAdjustment.objects.filter(
        lease_tenant__lease=active_lease, amount=Decimal("50.00")
    ).exists()
    assert done["adjustment"]["end_date"] == "2026-08-31"


def test_apply_rent_adjustment_targets_household_total_for_one_month(
    active_lease,
    landlord,
):
    preview = registry.execute(
        "apply_rent_adjustment",
        {
            "lease_number": active_lease.lease_number,
            "target_lease_total": "400",
            "reason": "One-time first-month rent adjustment",
            "effective_date": "2026-08-01",
            "is_recurring": "0",
        },
        landlord=landlord,
    )

    assert preview.get("needs_confirm"), preview
    assert preview["preview"]["current_lease_total"] == "1000.00"
    assert preview["preview"]["target_lease_total"] == "400.00"
    assert preview["preview"]["amount"] == "600.00"
    assert preview["preview"]["end_date"] == "2026-08-31"
    assert preview["resolved_arguments"]["lease_number"] == active_lease.lease_number
    assert preview["resolved_arguments"]["expected_current_total"] == "1000.00"

    done = registry.execute(
        "apply_rent_adjustment",
        {**preview["resolved_arguments"], "confirm": "yes"},
        landlord=landlord,
    )

    assert done.get("applied"), done
    assert done["previous_lease_total"] == "1000.00"
    assert done["target_lease_total"] == "400.00"
    adjustment = RentAdjustment.objects.get(lease_tenant__lease=active_lease)
    assert adjustment.amount == Decimal("600.00")
    assert adjustment.target_amount == Decimal("400.00")
    assert compute_joint_rent_for_due_date(
        active_lease, date(2026, 8, 1)
    ) == Decimal("400.00")
    assert compute_joint_rent_for_due_date(
        active_lease, date(2026, 9, 1)
    ) == Decimal("1000.00")


def test_target_total_overrides_proration_and_reconciles_open_charge(
    active_lease,
    landlord,
):
    active_lease.start_date = date(2026, 8, 4)
    active_lease.move_in_date = date(2026, 8, 4)
    active_lease.total_rent = Decimal("1900.00")
    active_lease.save(
        update_fields=["start_date", "move_in_date", "total_rent", "updated_at"],
    )
    slot = active_lease.lease_tenants.get()
    slot.rent_amount = Decimal("1900.00")
    slot.save(update_fields=["rent_amount", "updated_at"])
    RentAdjustment.create_proration(
        lease_tenant=slot,
        move_in_date=date(2026, 8, 4),
        period_end_date=date(2026, 8, 31),
        created_by=landlord,
    )
    original, created = ensure_joint_rent_charge(
        active_lease, date(2026, 8, 4),
    )
    assert created is True
    assert original.amount == Decimal("1716.13")

    preview = registry.execute(
        "apply_rent_adjustment",
        {
            "lease_number": active_lease.lease_number,
            "target_lease_total": "1900.00",
            "effective_date": "2026-08-04",
            "is_recurring": "0",
            "reason": "Override first-month proration",
        },
        landlord=landlord,
    )

    assert preview["preview"]["current_lease_total"] == "1716.13"
    assert preview["preview"]["target_lease_total"] == "1900.00"
    assert preview["preview"]["amount"] == "183.87"
    assert preview["resolved_arguments"]["expected_current_total"] == "1716.13"

    done = registry.execute(
        "apply_rent_adjustment",
        {**preview["resolved_arguments"], "confirm": "yes"},
        landlord=landlord,
    )

    assert done.get("applied"), done
    assert done["ledger"] == {"reposted": 1, "credited": 0}, done
    override = RentAdjustment.objects.exclude(
        adjustment_type=RentAdjustment.AdjustmentType.PRORATION,
    ).get(lease_tenant=slot)
    assert override.amount == Decimal("183.87")
    assert override.target_amount == Decimal("1900.00")
    assert compute_joint_rent_for_due_date(
        active_lease, date(2026, 8, 4),
    ) == Decimal("1900.00")
    assert LedgerEntry.objects.filter(
        lease=active_lease,
        entry_type=EntryType.RENT_CHARGE,
        reversed_by__isnull=True,
        due_date=date(2026, 8, 4),
        amount=Decimal("1900.00"),
    ).exists()


def test_apply_rent_adjustment_refuses_ambiguous_property_only_lookup(
    active_lease,
    landlord,
):
    Lease.objects.create(
        landlord=landlord,
        property=active_lease.property,
        lease_type=Lease.LeaseType.GENERIC_ROOMMATE,
        status=Lease.LeaseStatus.PENDING_SIGNATURES,
        start_date=date(2027, 1, 1),
        end_date=date(2027, 6, 30),
        is_month_to_month=False,
        total_rent=Decimal("1200.00"),
        security_deposit=Decimal("600.00"),
    )

    result = registry.execute(
        "apply_rent_adjustment",
        {
            "property_query": active_lease.property.name,
            "target_lease_total": "400",
            "effective_date": "2026-08-01",
        },
        landlord=landlord,
    )

    assert "Multiple open leases" in result["error"]
    assert "exact lease_number" in result["error"]


def test_record_utility_bill(active_lease, landlord):
    preview = registry.execute(
        "record_utility_bill",
        {
            "lease_number": active_lease.lease_number,
            "total_amount": "120.00",
            "period_start": "2026-07-01",
            "period_end": "2026-07-31",
            "description": "Hydro July",
        },
        landlord=landlord,
    )
    assert preview.get("needs_confirm"), preview

    done = registry.execute(
        "record_utility_bill",
        {
            "lease_number": active_lease.lease_number,
            "total_amount": "120.00",
            "period_start": "2026-07-01",
            "period_end": "2026-07-31",
            "description": "Hydro July",
            "confirm": "yes",
        },
        landlord=landlord,
    )
    assert done.get("recorded"), done
    assert done["count"] >= 1


def test_convert_inquiry_to_viewing(landlord):
    from rentium.showcase.models import Inquiry

    prop = Property.objects.create(
        landlord=landlord,
        name="Garden View",
        address="200 Oak",
        city="Victoria",
        province="bc",
        property_category=Property.PropertyCategory.COMPLETE_UNIT,
    )
    inq = Inquiry.objects.create(
        landlord=landlord,
        property=prop,
        name="Hitakshi",
        email="hitakshi@example.com",
        phone="2505550100",
        message="Interested in viewing next week",
    )

    preview = registry.execute(
        "convert_inquiry_to_viewing",
        {"inquiry_id": str(inq.pk), "when": "2026-08-10 15:00"},
        landlord=landlord,
    )
    assert preview.get("needs_confirm"), preview

    done = registry.execute(
        "convert_inquiry_to_viewing",
        {
            "inquiry_id": str(inq.pk),
            "when": "2026-08-10 15:00",
            "confirm": "yes",
        },
        landlord=landlord,
    )
    assert done.get("converted"), done
    inq.refresh_from_db()
    assert inq.status == Inquiry.Status.REPLIED
    assert inq.appointment_id is not None
