"""wipe_landlord_data — clears operational data, preserves the landlord account."""

from __future__ import annotations

import pytest
from django.core.management import call_command

from rentium.users.tests.factories import UserFactory


@pytest.fixture
def seeded(landlord, bc_property, bc_lease):
    """A landlord graph: property + active lease + tenant slot + inquiry +
    prospect conversation, plus the things a wipe must PRESERVE (showcase,
    channel link, RAMA prefs)."""
    from rentium.comms.models import ChannelAccount
    from rentium.messaging.models import Conversation
    from rentium.rama.models import RamaPreferences
    from rentium.showcase.models import Inquiry, Showcase
    from rentium.users.models import TenantProfile

    tenant_user = UserFactory(email="wipe.tenant@example.com")
    tenant = TenantProfile.objects.create(user=tenant_user)
    bc_lease.lease_tenants.create(tenant=tenant, rent_amount="850.00")

    Inquiry.objects.create(
        property=bc_property, landlord=landlord, name="Lead", email="lead@example.com",
        message="interested",
    )
    Conversation.objects.create(
        landlord=landlord, property=bc_property, prospect_email="lead@example.com",
        prospect_name="Lead",
    )
    # Preserve-me records:
    Showcase.objects.create(landlord=landlord, slug="wipe-test", is_public=True)
    ChannelAccount.objects.create(landlord=landlord, channel_type="TELEGRAM", address="123")
    RamaPreferences.objects.create(landlord=landlord)
    return {"landlord": landlord, "tenant_user_id": tenant_user.pk}


@pytest.mark.django_db
def test_dry_run_changes_nothing(seeded):
    from rentium.leases.models import Lease

    call_command("wipe_landlord_data", email=seeded["landlord"].user.email)
    assert Lease.objects.filter(landlord=seeded["landlord"]).exists()


@pytest.mark.django_db
def test_confirm_wipes_operations_preserves_account(seeded):
    from rentium.comms.models import ChannelAccount
    from rentium.leases.models import Lease
    from rentium.messaging.models import Conversation
    from rentium.properties.models import Property
    from rentium.rama.models import RamaPreferences
    from rentium.showcase.models import Inquiry, Showcase
    from rentium.users.models import LandlordProfile, User

    landlord = seeded["landlord"]
    call_command("wipe_landlord_data", email=landlord.user.email, confirm=True)

    # Operational data is gone.
    assert not Lease.objects.filter(landlord=landlord).exists()
    assert not Property.objects.filter(landlord=landlord).exists()
    assert not Inquiry.objects.filter(landlord=landlord).exists()
    assert not Conversation.objects.filter(landlord=landlord).exists()
    assert not User.objects.filter(pk=seeded["tenant_user_id"]).exists()  # test tenant removed

    # The landlord account and their settings survive.
    landlord.refresh_from_db()
    assert LandlordProfile.objects.filter(pk=landlord.pk).exists()
    assert User.objects.filter(pk=landlord.user_id).exists()
    assert Showcase.objects.filter(landlord=landlord).exists()
    assert ChannelAccount.objects.filter(landlord=landlord).exists()
    assert RamaPreferences.objects.filter(landlord=landlord).exists()


@pytest.mark.django_db
def test_keep_tenants_leaves_tenant_logins(seeded):
    from rentium.users.models import User

    call_command(
        "wipe_landlord_data",
        email=seeded["landlord"].user.email,
        confirm=True,
        keep_tenants=True,
    )
    assert User.objects.filter(pk=seeded["tenant_user_id"]).exists()
