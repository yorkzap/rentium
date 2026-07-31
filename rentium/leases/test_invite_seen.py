"""Landlord can see when a tenant last viewed the lease."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest
from rest_framework.test import APIClient

from rentium.leases.models import Lease, LeaseInviteEvent, LeaseTenant
from rentium.leases.services import invite_lifecycle, record_invite_event
from rentium.properties.models import Property
from rentium.users.models import TenantProfile
from rentium.users.tests.factories import UserFactory

pytestmark = pytest.mark.django_db


@pytest.fixture
def lease_with_invite(landlord):
    prop = Property.objects.create(
        landlord=landlord,
        name="Seen Room",
        address="1 Seen St",
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
        start_date=date(2026, 8, 1),
        end_date=date(2026, 12, 31),
        total_rent=Decimal("800.00"),
        security_deposit=Decimal("400.00"),
    )
    lt = LeaseTenant.objects.create(
        lease=lease,
        invited_email="seen@example.com",
        invited_name="Seen Tenant",
        rent_amount=Decimal("800.00"),
        is_primary_tenant=True,
    )
    return lease, lt


def test_invite_lifecycle_tracks_last_seen_and_count(lease_with_invite):
    lease, lt = lease_with_invite
    assert invite_lifecycle(lt)["has_seen_lease"] is False
    assert invite_lifecycle(lt)["last_seen_at"] is None

    record_invite_event(lt, LeaseInviteEvent.Kind.LINK_OPENED)
    record_invite_event(lt, LeaseInviteEvent.Kind.LINK_OPENED)
    life = invite_lifecycle(lt)
    assert life["has_seen_lease"] is True
    assert life["seen_count"] == 2
    assert life["last_seen_at"] is not None
    assert life["first_seen_at"] is not None
    assert life["last_seen_source"] == "invite_link"

    record_invite_event(
        lt, LeaseInviteEvent.Kind.LEASE_VIEWED, metadata={"via": "pdf"}
    )
    life = invite_lifecycle(lt)
    assert life["seen_count"] == 3
    assert life["last_seen_source"] == "pdf"


def test_lease_document_records_view_for_tenant(lease_with_invite, landlord):
    lease, lt = lease_with_invite
    user = UserFactory(email="seen@example.com")
    tenant = TenantProfile.objects.create(user=user)
    lt.tenant = tenant
    lt.save(update_fields=["tenant", "updated_at"])

    client = APIClient()
    client.force_authenticate(user=user)
    res = client.get(f"/api/leases/{lease.pk}/document/")
    assert res.status_code == 200
    assert lt.invite_events.filter(kind=LeaseInviteEvent.Kind.LEASE_VIEWED).exists()
    life = invite_lifecycle(lt)
    assert life["has_seen_lease"] is True
    assert life["last_seen_source"] == "agreement"


def test_api_serializer_exposes_last_seen(lease_with_invite, landlord):
    lease, lt = lease_with_invite
    record_invite_event(lt, LeaseInviteEvent.Kind.LINK_OPENED)
    client = APIClient()
    client.force_authenticate(user=landlord.user)
    res = client.get(f"/api/leases/{lease.pk}/")
    assert res.status_code == 200
    tenants = res.data.get("lease_tenants") or []
    assert tenants
    life = tenants[0].get("invite_lifecycle") or {}
    assert life.get("has_seen_lease") is True
    assert life.get("last_seen_at")


def test_rama_roster_seen_summary(lease_with_invite, landlord):
    from rentium.rama import registry

    lease, lt = lease_with_invite
    record_invite_event(lt, LeaseInviteEvent.Kind.LINK_OPENED)
    out = registry.execute(
        "list_lease_roster",
        {"lease_number": lease.lease_number},
        landlord=landlord,
    )
    assert out.get("seen_summary")
    row = out["seen_summary"][0]
    assert row["has_seen_lease"] is True
    assert row["last_seen_at"]
