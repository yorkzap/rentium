"""
User signals.

Auto-accept pending co-landlord invites: when someone signs up (or an account
otherwise appears) with an email that a landlord already invited as a
co-landlord, link that `LandlordTeamMember` to the new user and mark it accepted
so `users.access.owner_profiles_for` grants the managed portfolio on next login.

Without this, `add_co_landlord` only linked invitees who *already* had an account
at invite time — anyone invited before signing up stayed an unlinked, access-less
record forever.
"""

from __future__ import annotations

from django.db.models.signals import post_save
from django.dispatch import receiver
from django.utils import timezone

from .models import LandlordTeamMember, User


@receiver(post_save, sender=User, dispatch_uid="link_pending_co_landlord_invites")
def link_pending_co_landlord_invites(sender, instance, created, **kwargs):
    if not created or not instance.email:
        return
    LandlordTeamMember.objects.filter(
        invited_email__iexact=instance.email, member__isnull=True
    ).update(member=instance, accepted_at=timezone.now())

    # Link any lease co-signer slots invited to this email, so the freshly
    # signed-up co-landlord immediately sees the leases they must sign.
    from rentium.leases.models import LeaseLandlordSignatory

    LeaseLandlordSignatory.objects.filter(
        email__iexact=instance.email, member__isnull=True
    ).update(member=instance)
