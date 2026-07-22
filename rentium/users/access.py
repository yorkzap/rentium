"""
Access resolution for co-landlords / property managers.

ONE place decides what a user may act on, so co-landlord access can be applied
surface-by-surface and always fails CLOSED: a surface that doesn't consult these
helpers keeps scoping to the user's own profile and grants no extra access.

Scope model (users.LandlordTeamMember):
  - both scope fields null  → WHOLE portfolio of `owner` (office manager / partner)
  - scope_property set       → that property + every sibling in its PropertyGroup
  - scope_group set          → the whole PropertyGroup

Owner-level helpers (unchanged names, used by RAMA acting-as):
  - owner_profiles_for(user)      → LandlordProfiles the user may act on
  - accessible_landlord_ids(user) → their pks
  - acting_landlord(user, owner_id=None) → the single profile to WRITE as

Property/lease-level helpers (used by the dashboard viewsets):
  - accessible_properties(user)  → Property queryset (own + granted)
  - accessible_leases(user)      → Lease queryset (own + granted, group-aware)
"""

from __future__ import annotations


def _accepted_grants(user):
    """Accepted co-landlord grants for this user (queryset, possibly empty)."""
    from .models import LandlordTeamMember

    if user is None or not getattr(user, "is_authenticated", False):
        return LandlordTeamMember.objects.none()
    return LandlordTeamMember.objects.filter(
        member=user, accepted_at__isnull=False
    ).select_related("scope_property", "scope_group")


def owner_profiles_for(user):
    """LandlordProfiles this user may act on: their own (if any) + owners they
    co-manage via an ACCEPTED grant (any scope). Returns a queryset."""
    from .models import LandlordProfile

    if user is None or not getattr(user, "is_authenticated", False):
        return LandlordProfile.objects.none()

    ids: set = set()
    own = getattr(user, "landlord_profile", None)
    if own is not None:
        ids.add(own.pk)
    ids.update(_accepted_grants(user).values_list("owner_id", flat=True))
    return LandlordProfile.objects.filter(pk__in=ids)


def accessible_landlord_ids(user) -> list:
    """The pk list of owner profiles the user may act on. Empty = no access."""
    return list(owner_profiles_for(user).values_list("pk", flat=True))


def acting_landlord(user, owner_id=None):
    """The single profile the user acts AS (writes / RAMA). An explicit, allowed
    `owner_id` always wins. Otherwise: the user's OWN portfolio if it has any
    properties; else a co-managed portfolio that does (so a co-landlord whose own
    account is empty doesn't land on a blank portfolio and RAMA report '0
    listings'); else their own; else the single managed owner. None if no access."""
    profiles = list(owner_profiles_for(user))
    if not profiles:
        return None
    by_id = {str(p.pk): p for p in profiles}
    if owner_id and str(owner_id) in by_id:
        return by_id[str(owner_id)]

    own = getattr(user, "landlord_profile", None)
    own_in = own is not None and any(p.pk == own.pk for p in profiles)
    if own_in and own.properties.exists():
        return own
    # Own portfolio is empty (or the user is a pure manager) — prefer a co-managed
    # portfolio that actually has properties.
    for p in profiles:
        if (own is None or p.pk != own.pk) and p.properties.exists():
            return p
    return own if own_in else profiles[0]


def actable_portfolios(user):
    """The portfolios this user may act AS, for a 'managing: [owner ▾]' switcher.
    Each: {owner_id, name, is_own (primary landlord), property_count}. The user's
    own portfolio (where they are the PRIMARY landlord) is flagged is_own; the
    rest are portfolios they co-host as a secondary landlord."""
    own = getattr(user, "landlord_profile", None)
    rows = []
    for p in owner_profiles_for(user):
        rows.append(
            {
                "owner_id": str(p.pk),
                "name": (getattr(p.user, "name", "") or p.user.email),
                "is_own": own is not None and p.pk == own.pk,
                "property_count": p.properties.count(),
            }
        )
    rows.sort(key=lambda r: (not r["is_own"], r["name"].lower()))
    return rows


def _access_scopes(user):
    """Resolve a user's grants into concrete id sets:
      full_owner_ids  – owners whose ENTIRE portfolio is accessible
      property_ids    – individually-granted property pks
      group_ids       – granted group pks (property-scoped grants expand to their
                        group; group-scoped grants add the group directly)
    """
    full_owner_ids: set = set()
    property_ids: set = set()
    group_ids: set = set()
    for g in _accepted_grants(user):
        if g.scope_property_id is None and g.scope_group_id is None:
            full_owner_ids.add(g.owner_id)
        if g.scope_group_id is not None:
            group_ids.add(g.scope_group_id)
        if g.scope_property_id is not None:
            property_ids.add(g.scope_property_id)
            if g.scope_property is not None and g.scope_property.group_id:
                group_ids.add(g.scope_property.group_id)
    return full_owner_ids, property_ids, group_ids


def accessible_properties(user):
    """Property queryset the user may see: their own + every granted property
    (property grants expand to their whole group; group grants to the group;
    portfolio grants to all of that owner's properties)."""
    from django.db.models import Q

    from rentium.properties.models import Property

    if user is None or not getattr(user, "is_authenticated", False):
        return Property.objects.none()

    q = Q(pk__in=[])  # start empty
    own = getattr(user, "landlord_profile", None)
    if own is not None:
        q |= Q(landlord=own)
    full_owner_ids, property_ids, group_ids = _access_scopes(user)
    if full_owner_ids:
        q |= Q(landlord_id__in=full_owner_ids)
    if group_ids:
        q |= Q(group_id__in=group_ids)
    if property_ids:
        q |= Q(pk__in=property_ids)
    return Property.objects.filter(q).distinct()


def scope_q(user, *, landlord_field="landlord", property_field=None, lease_field=None):
    """A Q matching the rows of ANY model this user may access — the reusable
    primitive for wiring co-landlord access into a viewset's get_queryset().

    Pass the FK field names that exist on the target model:
      - landlord_field: matches the user's own records + records of owners whose
        WHOLE portfolio they were granted (None if the model has no landlord FK).
      - property_field / lease_field: match records tied to any property/lease the
        user can access (own + property/group-scoped grants), so a property-scoped
        co-landlord sees exactly that property's rows.

    A plain owner still matches everything (accessible_properties/leases include
    all their own), so this is safe as a drop-in for `filter(landlord=own)`.
    """
    from django.db.models import Q

    q = Q(pk__in=[])
    if user is None or not getattr(user, "is_authenticated", False):
        return q

    if landlord_field:
        full_owner_ids, _p, _g = _access_scopes(user)
        owner_ids = set(full_owner_ids)
        own = getattr(user, "landlord_profile", None)
        if own is not None:
            owner_ids.add(own.pk)
        if owner_ids:
            q |= Q(**{f"{landlord_field}_id__in": owner_ids})
    if property_field:
        q |= Q(**{f"{property_field}__in": accessible_properties(user)})
    if lease_field:
        q |= Q(**{f"{lease_field}__in": accessible_leases(user)})
    return q


def accessible_leases(user):
    """Lease queryset the user may see, scoped the same way as properties.
    Group-aware: a lease attached to a granted group (even with no property) is
    included, and older leases on a granted property are included for
    management/messaging even if the co-landlord isn't named on them."""
    from django.db.models import Q

    from rentium.leases.models import Lease

    if user is None or not getattr(user, "is_authenticated", False):
        return Lease.objects.none()

    q = Q(pk__in=[])
    own = getattr(user, "landlord_profile", None)
    if own is not None:
        q |= Q(landlord=own)
    full_owner_ids, property_ids, group_ids = _access_scopes(user)
    if full_owner_ids:
        q |= Q(landlord_id__in=full_owner_ids)
    if group_ids:
        q |= Q(group_id__in=group_ids) | Q(property__group_id__in=group_ids)
    if property_ids:
        q |= Q(property_id__in=property_ids)
    return Lease.objects.filter(q).distinct()
