"""
Fixing an invite that went to the wrong address.

    "there should be option to … remove the invites sent to the tenant or edit
     them (in case the landlord has sent it to wrong email or needs to change
     the tenant)"

Editing and deleting a slot already worked through the API. What did not was
the part that matters: changing `invited_email` left `invite_token` untouched,
so the stranger who received the first email kept a live link to read the whole
tenancy — the parties, the rent, the address — and to sign it as though they
were the tenant. Relabelling the row is not redirecting the invite.

So a redirect rotates the token, which kills the old link, and records that it
did. What it deliberately does NOT erase is the LINK_OPENED history: if the
wrong recipient opened it, that happened, and it is exactly the fact a landlord
needs afterwards.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest
from rest_framework.test import APIClient

from rentium.leases.models import Lease, LeaseInviteEvent, LeaseTenant

pytestmark = pytest.mark.django_db


@pytest.fixture
def lease(landlord, bc_property):
    return Lease.objects.create(
        landlord=landlord,
        property=bc_property,
        lease_type=Lease.LeaseType.BC_RESIDENTIAL_TENANCY,
        status=Lease.LeaseStatus.DRAFT,
        start_date=date(2026, 9, 1),
        end_date=date(2027, 8, 31),
        total_rent=Decimal("1800.00"),
    )


@pytest.fixture
def invited(lease):
    """A slot sent to the wrong address, already opened by whoever got it."""
    slot = LeaseTenant.objects.create(
        lease=lease,
        invited_email="wrong.person@example.com",
        invited_name="Siya Gulati",
        rent_amount=Decimal("1800.00"),
    )
    LeaseInviteEvent.objects.create(
        lease_tenant=slot, kind=LeaseInviteEvent.Kind.LINK_OPENED
    )
    return slot


def _client(landlord):
    client = APIClient()
    client.force_authenticate(user=landlord.user)
    return client


# ------------------------------------------------- redirecting the invite
def test_the_email_can_be_corrected(landlord, invited):
    response = _client(landlord).patch(
        f"/api/leases/tenants/{invited.pk}/",
        {"invited_email": "siyagulati900@gmail.com"},
        format="json",
    )
    assert response.status_code == 200, response.data
    invited.refresh_from_db()
    assert invited.invited_email == "siyagulati900@gmail.com"


def test_the_old_link_stops_working(landlord, invited):
    """The whole point. Without this the first recipient keeps a working link
    to a tenancy that was never theirs."""
    old_token = invited.invite_token

    _client(landlord).patch(
        f"/api/leases/tenants/{invited.pk}/",
        {"invited_email": "siyagulati900@gmail.com"},
        format="json",
    )

    invited.refresh_from_db()
    assert invited.invite_token != old_token

    stale = APIClient().get(
        f"/api/leases/tenants/{invited.pk}/invite-preview/?token={old_token}"
    )
    assert stale.status_code in (403, 404), stale.status_code


def test_the_new_link_works(landlord, invited):
    _client(landlord).patch(
        f"/api/leases/tenants/{invited.pk}/",
        {"invited_email": "siyagulati900@gmail.com"},
        format="json",
    )
    invited.refresh_from_db()

    fresh = APIClient().get(
        f"/api/leases/tenants/{invited.pk}/invite-preview/"
        f"?token={invited.invite_token}"
    )
    assert fresh.status_code == 200, fresh.data


def test_it_no_longer_claims_to_have_been_sent(landlord, invited):
    """It has not been sent to the new address. Leaving the old timestamp
    shows the landlord a delivery they never made."""
    invited.invite_sent_at = "2026-08-01T10:00:00Z"
    invited.save(update_fields=["invite_sent_at"])

    _client(landlord).patch(
        f"/api/leases/tenants/{invited.pk}/",
        {"invited_email": "siyagulati900@gmail.com"},
        format="json",
    )
    invited.refresh_from_db()
    assert invited.invite_sent_at is None


def test_the_redirect_is_on_the_record(landlord, invited):
    _client(landlord).patch(
        f"/api/leases/tenants/{invited.pk}/",
        {"invited_email": "siyagulati900@gmail.com"},
        format="json",
    )
    event = invited.invite_events.get(
        kind=LeaseInviteEvent.Kind.INVITE_REDIRECTED
    )
    assert event.metadata["from"] == "wrong.person@example.com"
    assert event.metadata["to"] == "siyagulati900@gmail.com"


def test_the_wrong_recipients_access_is_not_erased(landlord, invited):
    """They DID open it. Deleting that would destroy the one record of who
    saw the tenancy before it was redirected."""
    _client(landlord).patch(
        f"/api/leases/tenants/{invited.pk}/",
        {"invited_email": "siyagulati900@gmail.com"},
        format="json",
    )
    assert invited.invite_events.filter(
        kind=LeaseInviteEvent.Kind.LINK_OPENED
    ).exists()


# --------------------------------------------------- what it leaves alone
def test_an_unchanged_email_rotates_nothing(landlord, invited):
    """A PATCH that touches the name must not silently invalidate a link the
    tenant is holding."""
    token = invited.invite_token
    _client(landlord).patch(
        f"/api/leases/tenants/{invited.pk}/",
        {"invited_name": "Siya G Gulati"},
        format="json",
    )
    invited.refresh_from_db()
    assert invited.invite_token == token
    assert not invited.invite_events.filter(
        kind=LeaseInviteEvent.Kind.INVITE_REDIRECTED
    ).exists()


def test_the_same_email_in_different_case_is_not_a_redirect(landlord, invited):
    token = invited.invite_token
    _client(landlord).patch(
        f"/api/leases/tenants/{invited.pk}/",
        {"invited_email": "Wrong.Person@Example.com"},
        format="json",
    )
    invited.refresh_from_db()
    assert invited.invite_token == token


def test_a_signed_slot_keeps_its_token(landlord, invited):
    """A signature is against this row. Rotating the token underneath a person
    who has already signed would lock them out of their own agreement."""
    invited.has_signed = True
    invited.save(update_fields=["has_signed"])
    token = invited.invite_token

    _client(landlord).patch(
        f"/api/leases/tenants/{invited.pk}/",
        {"invited_email": "someone.else@example.com"},
        format="json",
    )
    invited.refresh_from_db()
    assert invited.invite_token == token


# ------------------------------------------------------ removing an invite
def test_an_invite_can_be_withdrawn(landlord, invited):
    response = _client(landlord).delete(f"/api/leases/tenants/{invited.pk}/")
    assert response.status_code == 204
    assert not LeaseTenant.objects.filter(pk=invited.pk).exists()


def test_another_landlord_cannot_touch_the_invite(invited):
    from rentium.users.models import LandlordProfile
    from rentium.users.tests.factories import UserFactory

    stranger = LandlordProfile.objects.create(user=UserFactory())
    response = _client(stranger).patch(
        f"/api/leases/tenants/{invited.pk}/",
        {"invited_email": "attacker@example.com"},
        format="json",
    )
    assert response.status_code in (403, 404)
    invited.refresh_from_db()
    assert invited.invited_email == "wrong.person@example.com"
