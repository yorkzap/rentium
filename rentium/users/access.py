"""
Owner-scope resolution for co-landlords / property managers.

ONE place decides "which landlord portfolios may this user act on", so the
co-landlord feature can be applied surface-by-surface and always fails CLOSED:
a viewset that hasn't been switched to these helpers keeps scoping to the user's
own profile and grants no extra access.

  - owner_profiles_for(user)      → every LandlordProfile the user may touch
  - accessible_landlord_ids(user) → their pks, for READ querysets
                                     (Model.objects.filter(landlord_id__in=...))
  - acting_landlord(user, owner_id=None) → the SINGLE profile to act AS for a
                                     WRITE (create/update). Own profile by
                                     default; a managed owner if the user has no
                                     own profile or explicitly selects one they
                                     are allowed to.
"""

from __future__ import annotations


def owner_profiles_for(user):
    """LandlordProfiles this user may act on: their own (if any) + owners they
    co-manage via an ACCEPTED team membership. Returns a queryset (possibly
    empty)."""
    from .models import LandlordProfile, LandlordTeamMember

    if user is None or not getattr(user, "is_authenticated", False):
        return LandlordProfile.objects.none()

    ids: set = set()
    own = getattr(user, "landlord_profile", None)
    if own is not None:
        ids.add(own.pk)
    ids.update(
        LandlordTeamMember.objects.filter(
            member=user, accepted_at__isnull=False
        ).values_list("owner_id", flat=True)
    )
    return LandlordProfile.objects.filter(pk__in=ids)


def accessible_landlord_ids(user) -> list:
    """The pk list for read scoping. Empty = no access (fails closed)."""
    return list(owner_profiles_for(user).values_list("pk", flat=True))


def acting_landlord(user, owner_id=None):
    """The single profile the user acts AS (writes / RAMA). Own profile by
    default; a specific managed owner if `owner_id` is one they're allowed to;
    a pure manager with no own profile acts as the owner they manage. None if
    the user has no landlord access at all."""
    profiles = list(owner_profiles_for(user))
    if not profiles:
        return None
    by_id = {str(p.pk): p for p in profiles}
    if owner_id and str(owner_id) in by_id:
        return by_id[str(owner_id)]
    own = getattr(user, "landlord_profile", None)
    if own is not None:
        return own
    return profiles[0]  # a pure manager managing a single owner
