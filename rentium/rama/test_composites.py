"""Phase 1 composites: renew, move-out, inspection package, rent adjust,
utility bill, inquiry→viewing."""

from __future__ import annotations

from datetime import date
from datetime import timedelta
from decimal import Decimal

import pytest

from rentium.leases.models import Lease, LeaseTenant, RentAdjustment
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
