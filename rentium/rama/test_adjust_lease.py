"""PENDING leases can have start date and furnishing adjusted without recreate."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from rentium.leases.models import Lease
from rentium.properties.models import InventoryItem
from rentium.properties.models import Property
from rentium.rama import registry
from rentium.rama.capabilities import supported_tool_for_request

pytestmark = pytest.mark.django_db


@pytest.fixture
def pending_lease(landlord):
    room = Property.objects.create(
        landlord=landlord,
        name="Room D",
        address="950 McKenzie Ave",
        city="Victoria",
        province="bc",
        property_category=Property.PropertyCategory.ROOM,
        room_type=Property.RoomType.PRIVATE,
    )
    lease = Lease.objects.create(
        landlord=landlord,
        property=room,
        lease_type=Lease.LeaseType.BC_ROOMMATE_AGREEMENT,
        status=Lease.LeaseStatus.PENDING_SIGNATURES,
        start_date=date(2026, 8, 1),
        end_date=date(2026, 12, 31),
        is_month_to_month=False,
        total_rent=Decimal("800.00"),
        security_deposit=Decimal("400.00"),
    )
    lease.lease_number = "RMT415536-0617"
    lease.save(update_fields=["lease_number"])
    return lease


def test_pending_lease_start_date_and_semi_furnished(pending_lease, landlord):
    assert pending_lease.is_locked() is False
    assert pending_lease.property.is_furnished is False

    preview = registry.execute(
        "adjust_lease",
        {
            "lease_number": "RMT415536-0617",
            "start_date": "2026-09-01",
            "furnishing": "semi_furnished",
        },
        landlord=landlord,
    )
    assert preview.get("needs_confirm"), preview
    assert preview["preview"]["lease_changes"]["start_date"] == "2026-09-01"
    assert "Queen bed" in (preview["preview"]["inventory_to_add"] or [])

    done = registry.execute(
        "adjust_lease",
        {
            "lease_number": "RMT415536-0617",
            "start_date": "2026-09-01",
            "furnishing": "semi_furnished",
            "confirm": "yes",
        },
        landlord=landlord,
    )
    assert done.get("updated"), done
    pending_lease.refresh_from_db()
    pending_lease.property.refresh_from_db()
    assert pending_lease.start_date == date(2026, 9, 1)
    assert pending_lease.end_date == date(2026, 12, 31)
    assert pending_lease.property.furnishing_status == "SEMI_FURNISHED"
    assert pending_lease.property.is_furnished is True
    assert InventoryItem.objects.filter(
        property=pending_lease.property, name__icontains="bed"
    ).exists()
    assert "lease" in done["message"].casefold()
    assert "RMT415536-0617" in done["message"]


def test_update_lease_alone_can_change_pending_start(pending_lease, landlord):
    out = registry.execute(
        "update_lease",
        {
            "lease_number": "RMT415536-0617",
            "start_date": "2026-09-01",
            "confirm": "yes",
        },
        landlord=landlord,
    )
    assert out.get("updated"), out
    pending_lease.refresh_from_db()
    assert pending_lease.start_date == date(2026, 9, 1)


def test_capability_maps_furnishing_and_start_date_to_adjust_lease():
    assert (
        supported_tool_for_request(
            "change the lease start date from 2026-08-01 to 2026-09-01 and make it semi furnished"
        )
        == "adjust_lease"
    )
