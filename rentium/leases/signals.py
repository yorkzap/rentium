"""
Lease signals.

When a lease is created on a property/group that has co-landlords, add each of
them as a signing party (LeaseLandlordSignatory) so future leases automatically
carry the co-landlord — matching the "invite a co-landlord to a property → every
future lease on it names them too" behaviour.
"""

from __future__ import annotations

from django.db.models.signals import post_save
from django.dispatch import receiver

from .models import Lease
from .services import sync_lease_landlord_signatories


@receiver(post_save, sender=Lease, dispatch_uid="attach_co_landlords_to_new_lease")
def attach_co_landlords_to_new_lease(sender, instance, created, **kwargs):
    if not created:
        return
    sync_lease_landlord_signatories(instance)
