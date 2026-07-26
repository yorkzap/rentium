"""Area helpers.

This module used to define a second area model (`Area`) alongside
PropertyArea. That model never held a row — the signals meant to seed it were
never connected (see rentium/ledger/apps.py) — while PropertyArea held the real
data and the legally load-bearing fields. The two were merged into
PropertyArea; only these helpers remain, now operating on it.

A tenant's covered territory = their room + the common areas of whatever
contains it + areas EXCLUSIVE to their room. Maintenance work orders reference
an area so tenants see exactly the tickets that affect them.
"""

from django.db import models

from .models import PropertyArea

# (name, area_type, kind) — the spaces a landlord almost certainly has, so they
# start with something to rename rather than a blank slate.
DEFAULT_COMMON_AREAS = [
    ("Kitchen", PropertyArea.AreaType.KITCHEN),
    ("Bathroom", PropertyArea.AreaType.BATHROOM),
    ("Living Room", PropertyArea.AreaType.LIVING_ROOM),
    ("Laundry", PropertyArea.AreaType.LAUNDRY),
    ("Hallway", PropertyArea.AreaType.HALLWAY),
    ("Exterior / Yard", PropertyArea.AreaType.GARDEN),
]
DEFAULT_SYSTEM_AREAS = [
    ("Heating / Furnace", PropertyArea.AreaType.HEATING),
    ("Hot Water", PropertyArea.AreaType.HOT_WATER),
    ("Electrical Panel", PropertyArea.AreaType.ELECTRICAL),
    ("Roof / Structure", PropertyArea.AreaType.ROOF),
]


def seed_default_areas(*, group=None, property=None, unit=None) -> list[PropertyArea]:
    """Give a new unit/group/property a starting set of named spaces.

    Idempotent by (parent, name), so it is safe to call on every save and safe
    to re-run as a backfill.
    """
    if group is not None:
        target = {"group": group}
    elif unit is not None:
        target = {"unit": unit}
    else:
        target = {"property": property}

    created: list[PropertyArea] = []
    for names, kind in (
        (DEFAULT_COMMON_AREAS, PropertyArea.Kind.COMMON),
        (DEFAULT_SYSTEM_AREAS, PropertyArea.Kind.SYSTEM),
    ):
        for name, area_type in names:
            area, was_created = PropertyArea.objects.get_or_create(
                **target,
                name=name,
                defaults={
                    "area_type": area_type,
                    "kind": kind,
                    "is_seeded_default": True,
                    "is_group_common": kind == PropertyArea.Kind.COMMON
                    and group is not None,
                },
            )
            if was_created:
                created.append(area)
    return created


def areas_for_tenant_room(room):
    """The areas a tenant renting `room` can see and report on.

    Their own exclusive areas, plus the common/system areas of whatever
    contains the room — its group if it is let room-by-room, its unit if the
    unit is let whole, else the listing itself.
    """
    shared_kinds = [PropertyArea.Kind.COMMON, PropertyArea.Kind.SYSTEM]
    q = models.Q(exclusive_to=room)
    if getattr(room, "group_id", None):
        q |= models.Q(group_id=room.group_id, kind__in=shared_kinds)
    if getattr(room, "unit_id", None):
        q |= models.Q(unit_id=room.unit_id, kind__in=shared_kinds)
    if not room.group_id and not room.unit_id:
        q |= models.Q(property=room, kind__in=shared_kinds)
    else:
        # Areas recorded directly against the listing are always the tenant's.
        q |= models.Q(property=room)
    return PropertyArea.objects.filter(q).distinct()
