"""Editing a lease that isn't executed yet.

A lease is editable until `Lease.is_locked()` — ACTIVE and beyond. Before then
it can already carry signatures: the landlord's, and any tenant who signed
early. Editing is allowed in that state, deliberately (the landlord owns the
document until it is executed), but a material change writes an immutable
TERMS_AMENDED event against everyone who had already signed. Nothing is sent to
them — the landlord decides who to tell; the record exists so they can see, and
later prove, who agreed to what.

These tests lock in the two halves of that: the edit goes through, and the
evidence gets written. Plus the ACTIVE lock, which is what stops an executed
lease and its posted ledger charges drifting apart.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest
from rest_framework.test import APIClient

from rentium.leases.models import Lease, LeaseInviteEvent, LeaseTenant

pytestmark = pytest.mark.django_db


@pytest.fixture
def pending_lease(landlord, bc_property):
    lease = Lease.objects.create(
        landlord=landlord,
        property=bc_property,
        lease_type=Lease.LeaseType.BC_RESIDENTIAL_TENANCY,
        status=Lease.LeaseStatus.PENDING_SIGNATURES,
        start_date=date(2026, 9, 1),
        end_date=date(2027, 8, 31),
        total_rent=Decimal("1800.00"),
        security_deposit=Decimal("200.00"),
        cleaning_deposit=Decimal("200.00"),
    )
    LeaseTenant.objects.create(
        lease=lease,
        invited_email="early@example.com",
        invited_name="Early Signer",
        rent_amount=Decimal("900.00"),
        is_primary_tenant=True,
        has_signed=True,
        signed_date=date(2026, 7, 12),
    )
    LeaseTenant.objects.create(
        lease=lease,
        invited_email="late@example.com",
        invited_name="Not Yet",
        rent_amount=Decimal("900.00"),
    )
    return lease


def _client(landlord):
    client = APIClient()
    client.force_authenticate(user=landlord.user)
    return client


def _amendments(lease):
    return LeaseInviteEvent.objects.filter(
        lease_tenant__lease=lease,
        kind=LeaseInviteEvent.Kind.TERMS_AMENDED,
    )


def test_a_pending_lease_is_editable_even_with_a_signature_on_it(
    landlord, pending_lease
):
    response = _client(landlord).patch(
        f"/api/leases/{pending_lease.pk}/",
        {"cleaning_deposit": "250.00", "total_rent": "1900.00"},
        format="json",
    )

    assert response.status_code == 200, response.data
    pending_lease.refresh_from_db()
    assert pending_lease.cleaning_deposit == Decimal("250.00")
    assert pending_lease.total_rent == Decimal("1900.00")


def test_amending_terms_records_it_against_whoever_already_signed(
    landlord, pending_lease
):
    response = _client(landlord).patch(
        f"/api/leases/{pending_lease.pk}/",
        {"cleaning_deposit": "250.00"},
        format="json",
    )

    assert response.data["amended_signers"] == ["Early Signer"]
    events = list(_amendments(pending_lease))
    assert len(events) == 1, "one event, for the one tenant who had signed"
    assert events[0].lease_tenant.invited_email == "early@example.com"
    assert events[0].metadata["fields"] == ["cleaning_deposit"]
    assert events[0].metadata["before"] == {"cleaning_deposit": "200.00"}
    assert events[0].metadata["after"] == {"cleaning_deposit": "250.00"}


def test_the_tenant_is_not_notified_of_an_amendment(landlord, pending_lease, mailoutbox):
    """Landlord control is the product decision: the record is written, the
    email is not sent."""
    _client(landlord).patch(
        f"/api/leases/{pending_lease.pk}/",
        {"total_rent": "1900.00"},
        format="json",
    )
    assert mailoutbox == []


def test_a_cosmetic_edit_is_not_an_amendment(landlord, pending_lease):
    """Fixing the notice address isn't changing the deal, so it doesn't
    accuse anyone of having signed something different."""
    response = _client(landlord).patch(
        f"/api/leases/{pending_lease.pk}/",
        {"landlord_service_address": "12 New Office Rd"},
        format="json",
    )

    assert response.status_code == 200, response.data
    assert response.data["amended_signers"] == []
    assert not _amendments(pending_lease).exists()


def test_an_active_lease_cannot_be_edited(landlord, bc_lease):
    """bc_lease is ACTIVE: its document is frozen and its deposit and rent
    charges are already posted. LeaseNotLocked refuses before anything else."""
    response = _client(landlord).patch(
        f"/api/leases/{bc_lease.pk}/",
        {"total_rent": "9999.00"},
        format="json",
    )

    assert response.status_code == 403
    bc_lease.refresh_from_db()
    assert bc_lease.total_rent == Decimal("850.00")


def test_status_cannot_be_patched_straight_to_active(landlord, pending_lease):
    """Activation runs check_and_activate(), which freezes the signed document,
    posts the deposit and rent charges and opens occupancy. PATCHing the field
    skipped all three."""
    response = _client(landlord).patch(
        f"/api/leases/{pending_lease.pk}/",
        {"status": "ACTIVE"},
        format="json",
    )

    assert response.status_code == 200
    pending_lease.refresh_from_db()
    assert pending_lease.status == Lease.LeaseStatus.PENDING_SIGNATURES


def test_agreement_terms_fields_are_now_reachable_from_the_api(
    landlord, pending_lease
):
    """rent_due_day and the parking block print into the agreement but had no
    route in from anywhere but Django admin."""
    response = _client(landlord).patch(
        f"/api/leases/{pending_lease.pk}/",
        {
            "rent_due_day": 15,
            "parking_included": True,
            "parking_description": "One stall, #14",
            # pets_terms is refused unless pets are actually allowed — the
            # model won't let the agreement contradict itself.
            "pets_allowed": True,
            "pets_terms": "One cat under 15lb",
        },
        format="json",
    )

    assert response.status_code == 200, response.data
    pending_lease.refresh_from_db()
    assert pending_lease.rent_due_day == 15
    assert pending_lease.parking_included is True
    assert pending_lease.parking_description == "One stall, #14"
    assert pending_lease.pets_terms == "One cat under 15lb"


def test_changing_a_signed_tenants_rent_share_is_allowed_and_recorded(
    landlord, pending_lease
):
    """It used to be refused outright, leaving Django admin as the only route.
    It is an amendment now, not a wall."""
    slot = pending_lease.lease_tenants.get(invited_email="early@example.com")
    response = _client(landlord).patch(
        f"/api/leases/tenants/{slot.pk}/",
        {"rent_amount": "1000.00"},
        format="json",
    )

    assert response.status_code == 200, response.data
    slot.refresh_from_db()
    assert slot.rent_amount == Decimal("1000.00")
    event = _amendments(pending_lease).get()
    assert event.metadata["after"] == {"rent_amount": "1000.00"}


def test_rama_and_the_dashboard_share_one_edit_path(landlord, pending_lease):
    """The amendment record can't be dodged by editing from the other door."""
    from rentium.rama import registry

    result = registry.execute(
        "update_lease",
        {
            "lease_number": pending_lease.lease_number,
            "cleaning_deposit": "300.00",
            "confirm": "yes",
        },
        landlord=landlord,
    )

    assert result["updated"] is True
    assert result["amended_signers"] == ["Early Signer"]
    assert "not been notified" in result["message"]
    assert _amendments(pending_lease).count() == 1
