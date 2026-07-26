"""Property hierarchy and shared-area services.

``PropertyArea`` remains the legal/common-area record used by the dashboard and
tenancy rules.  All group-wide membership changes go through this module so a
room cannot remain attached to the common areas of a group it left.
"""

from __future__ import annotations

import re
from difflib import SequenceMatcher

from django.core.exceptions import ValidationError
from django.db import transaction

from .models import Property
from .models import PropertyArea
from .models import PropertyGroup
from .models import PropertyUnit

AREA_ALIASES = {
    "kitchen": PropertyArea.AreaType.KITCHEN,
    "bathroom": PropertyArea.AreaType.BATHROOM,
    "bath": PropertyArea.AreaType.BATHROOM,
    "living room": PropertyArea.AreaType.LIVING_ROOM,
    "living": PropertyArea.AreaType.LIVING_ROOM,
    "dining room": PropertyArea.AreaType.DINING_ROOM,
    "dining": PropertyArea.AreaType.DINING_ROOM,
    "laundry": PropertyArea.AreaType.LAUNDRY,
    "office": PropertyArea.AreaType.OFFICE,
    "den": PropertyArea.AreaType.OFFICE,
    "balcony": PropertyArea.AreaType.BALCONY,
    "patio": PropertyArea.AreaType.BALCONY,
    "hallway": PropertyArea.AreaType.HALLWAY,
    "entryway": PropertyArea.AreaType.HALLWAY,
    "storage": PropertyArea.AreaType.STORAGE,
    "garage": PropertyArea.AreaType.GARAGE,
    "garden": PropertyArea.AreaType.GARDEN,
    "yard": PropertyArea.AreaType.GARDEN,
}
NEAR_DUPLICATE_THRESHOLD = 0.84


def parse_common_area_types(raw: str) -> list[str]:
    """Parse JSON-ish/comma prose into canonical ``PropertyArea`` type codes."""
    text = (raw or "").strip()
    if not text:
        return []
    lowered = re.sub(r"[_-]+", " ", text.casefold())
    found: list[str] = []
    # Longest aliases first so "living room" wins over "living".
    for alias, area_type in sorted(
        AREA_ALIASES.items(), key=lambda item: len(item[0]), reverse=True,
    ):
        if re.search(rf"\b{re.escape(alias)}s?\b", lowered) and area_type not in found:
            found.append(area_type)
    for code, _label in PropertyArea.AreaType.choices:
        if code.casefold().replace("_", " ") in lowered and code not in found:
            found.append(code)
    return found


def _normalise_listing_name(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", (value or "").casefold())


def listing_name_conflicts(landlord, name: str, *, exclude_id=None) -> list[dict]:
    """Return exact and close listing-name matches for previews.

    SequenceMatcher deliberately catches small spelling changes such as
    ``McKenzie B`` versus ``Mackenzie B`` without treating every shared suffix
    (for example, ``Room A``/``Room B``) as a conflict.
    """
    wanted = _normalise_listing_name(name)
    if not wanted:
        return []
    qs = Property.objects.filter(landlord=landlord)
    if exclude_id is not None:
        qs = qs.exclude(pk=exclude_id)
    conflicts: list[dict] = []
    for prop in qs.select_related("group", "holding").order_by("created_at"):
        candidate = _normalise_listing_name(prop.name)
        ratio = SequenceMatcher(None, wanted, candidate).ratio()
        exact = wanted == candidate
        if not exact and ratio < NEAR_DUPLICATE_THRESHOLD:
            continue
        conflicts.append(
            {
                "id": str(prop.pk),
                "name": prop.name,
                "match": "exact" if exact else "near",
                "similarity": round(ratio, 2),
                "group": prop.group.name if prop.group_id else None,
                "holding": prop.holding.name if prop.holding_id else None,
                "address": prop.address,
            },
        )
    return conflicts


def group_common_areas(group: PropertyGroup):
    """The durable set of common areas for a group."""
    return (
        PropertyArea.objects.filter(
            property__group=group,
            is_group_common=True,
        )
        .select_related("property")
        .prefetch_related("shared_by")
        .order_by("area_type", "id")
    )


def _group_rooms(group: PropertyGroup) -> list[Property]:
    return list(
        group.grouped_properties.filter(
            property_category=Property.PropertyCategory.ROOM,
        ).order_by("created_at", "pk"),
    )


@transaction.atomic
def sync_group_common_areas(group: PropertyGroup) -> list[PropertyArea]:
    """Idempotently associate every common area with every current group room."""
    rooms = _group_rooms(group)
    room_ids = [room.pk for room in rooms]
    areas = list(group_common_areas(group).select_for_update())
    if not rooms:
        for area in areas:
            area.shared_by.clear()
        return areas

    primary = rooms[0]
    member_ids = set(room_ids)
    for area in areas:
        if area.property_id not in member_ids:
            area.property = primary
            area.save(update_fields=["property", "updated_at"])
        area.shared_by.set(room_ids)
    return areas


@transaction.atomic
def sync_room_group_membership(
    room: Property,
    *,
    old_group: PropertyGroup | None = None,
) -> None:
    """Repair common-area membership after a room is added, moved, or removed."""
    if room.property_category != Property.PropertyCategory.ROOM:
        for area in PropertyArea.objects.filter(shared_by=room):
            area.shared_by.remove(room)
        return

    current_group = room.group
    if old_group is not None and (
        current_group is None or old_group.pk != current_group.pk
    ):
        # A common area must not leave the old group merely because the room that
        # originally owned its FK moved. Re-home it to the oldest remaining room.
        remaining = _group_rooms(old_group)
        replacement = remaining[0] if remaining else None
        owned_common = PropertyArea.objects.filter(
            property=room,
            is_group_common=True,
        ).select_for_update()
        for area in owned_common:
            if replacement is not None:
                area.property = replacement
                area.save(update_fields=["property", "updated_at"])
            else:
                # No group remains. Keep the record with the room, but it is no
                # longer a group-wide common area.
                area.is_group_common = False
                area.save(update_fields=["is_group_common", "updated_at"])
                area.shared_by.clear()

    # Remove stale memberships first, regardless of which room owned the area.
    for area in PropertyArea.objects.filter(shared_by=room).select_for_update():
        area.shared_by.remove(room)

    if old_group is not None and (
        current_group is None or old_group.pk != current_group.pk
    ):
        sync_group_common_areas(old_group)
    if current_group is not None:
        sync_group_common_areas(current_group)


@transaction.atomic
def assign_room_to_group(
    room: Property,
    group: PropertyGroup | None,
) -> Property:
    """Set a room's group and synchronize both old and new common areas."""
    room = Property.objects.select_for_update().get(pk=room.pk)
    if room.property_category != Property.PropertyCategory.ROOM:
        message = "Only ROOM listings can belong to a property group."
        raise ValidationError(message)
    if group is not None and group.landlord_id != room.landlord_id:
        message = "The room and group must belong to the same landlord."
        raise ValidationError(message)
    old_group = (
        PropertyGroup.objects.filter(pk=room.group_id).first()
        if room.group_id
        else None
    )
    if (old_group.pk if old_group else None) == (group.pk if group else None):
        if group is not None:
            sync_group_common_areas(group)
        return room

    # The post-save signal also calls the synchronizer. The explicit call keeps
    # this service correct even when signals are disabled in a data migration.
    room.group = group
    room.full_clean()
    room.save(update_fields=["group", "updated_at"])
    sync_room_group_membership(room, old_group=old_group)
    return room


@transaction.atomic
def create_group_common_area(
    group: PropertyGroup,
    *,
    area_type: str,
    count: int = 1,
    description: str = "",
    shared_with_landlord: bool,
) -> tuple[PropertyArea, bool]:
    """Create or idempotently return one group-wide common area."""
    group = PropertyGroup.objects.select_for_update().get(pk=group.pk)
    rooms = _group_rooms(group)
    if not rooms:
        message = "Add a room to the group before creating common areas."
        raise ValidationError(message)
    if area_type not in PropertyArea.AreaType.values:
        raise ValidationError(
            {"area_type": f"Must be one of: {', '.join(PropertyArea.AreaType.values)}"},
        )
    area = (
        PropertyArea.objects.select_for_update()
        .filter(
            property__group=group,
            area_type=area_type,
            is_group_common=True,
        )
        .order_by("created_at", "pk")
        .first()
    )
    created = area is None
    if created:
        area = PropertyArea(
            property=rooms[0],
            area_type=area_type,
            count=max(int(count), 1),
            description=(description or "").strip(),
            shared_with_landlord=shared_with_landlord,
            is_group_common=True,
        )
    else:
        area.count = max(int(count), 1)
        area.description = (description or area.description or "").strip()
        area.shared_with_landlord = shared_with_landlord
        area.is_group_common = True
    area.full_clean()
    area.save()
    area.shared_by.set([room.pk for room in rooms])
    return area, created


@transaction.atomic
def update_group_common_area(
    group: PropertyGroup,
    area: PropertyArea,
    *,
    count: int | None = None,
    description: str | None = None,
    shared_with_landlord: bool | None = None,
) -> PropertyArea:
    """Update a common area while preserving group-wide membership."""
    area = PropertyArea.objects.select_for_update().get(
        pk=area.pk,
        property__group=group,
        is_group_common=True,
    )
    if count is not None:
        area.count = max(int(count), 1)
    if description is not None:
        area.description = description.strip()
    if shared_with_landlord is not None:
        area.shared_with_landlord = shared_with_landlord
    area.full_clean()
    area.save()
    sync_group_common_areas(group)
    return area


# --------------------------------------------------------------- rental mode
# Switching how a unit is rented is reversible and never destructive. The rule
# is simple and absolute: nothing is deleted. Listings belonging to the mode a
# unit is leaving are PARKED (is_active_offering=False), keeping their photos,
# description, inventory and lease history, so switching back reuses the
# original rather than creating a near-duplicate.
#
# A switch is blocked outright while any lease is live anywhere in the unit.
# Re-shaping what is on offer underneath a signed or half-signed agreement is
# how you end up with a tenancy pointing at a listing that no longer means what
# it meant when it was signed.

class RentalModeError(ValidationError):
    """Raised when a rental-mode switch is not safe to perform."""


# DRAFT and PENDING count: a draft is paperwork someone is mid-way through, and
# a pending lease is already out for signature.
BLOCKING_LEASE_STATUSES = ("DRAFT", "PENDING", "ACTIVE")


def _unit_listings(unit: PropertyUnit):
    return Property.objects.filter(unit=unit)


def blocking_leases_for_unit(unit: PropertyUnit) -> list:
    """Every live lease anywhere in this unit — on any of its listings, or on
    its room-group. Empty list = the unit is free to be restructured."""
    from django.db.models import Q

    from rentium.leases.models import Lease

    scope = Q(property__unit=unit)
    group = getattr(unit, "room_group", None)
    if group is not None:
        scope |= Q(group=group)
    return list(
        Lease.objects.filter(scope, status__in=BLOCKING_LEASE_STATUSES)
        .select_related("property")
        .distinct()
    )


def describe_rental_mode_switch(unit: PropertyUnit, new_mode: str) -> dict:
    """What WOULD happen, without touching anything.

    Returned verbatim to the landlord (and to RAMA as a preview) so a switch is
    always shown before it is run.
    """
    new_mode = (new_mode or "").strip().upper()
    valid = {m for m, _label in PropertyUnit.RentalMode.choices}
    if new_mode not in valid:
        return {
            "ok": False,
            "error": f"rental_mode must be one of {sorted(valid)} (got {new_mode!r}).",
        }

    blockers = blocking_leases_for_unit(unit)
    if new_mode == unit.rental_mode:
        return {
            "ok": False,
            "error": f"{unit.name} is already rented {unit.get_rental_mode_display().lower()}.",
        }

    to_park = list(_unit_listings(unit).filter(is_active_offering=True))
    wanted_category = (
        Property.PropertyCategory.COMPLETE_UNIT
        if new_mode == PropertyUnit.RentalMode.WHOLE_UNIT
        else Property.PropertyCategory.ROOM
    )
    to_revive = list(
        _unit_listings(unit).filter(
            is_active_offering=False, property_category=wanted_category
        )
    )

    return {
        "ok": not blockers,
        "unit": unit.name,
        "from_mode": unit.rental_mode,
        "to_mode": new_mode,
        "blocked_by": [
            {
                "lease_number": lease.lease_number,
                "status": lease.status,
                "listing": lease.property.name if lease.property_id else None,
            }
            for lease in blockers
        ],
        "will_park": [p.name for p in to_park],
        "will_reactivate": [p.name for p in to_revive],
        "needs_new_listing": not to_revive,
        "note": (
            "Nothing is deleted. Parked listings keep their photos, inventory "
            "and history, and come back if you switch this unit back."
        ),
    }


@transaction.atomic
def set_rental_mode(unit: PropertyUnit, new_mode: str) -> dict:
    """Switch how a unit is rented. Raises RentalModeError if not safe.

    Idempotent in the sense that matters: it refuses a no-op switch rather
    than silently re-parking listings.
    """
    preview = describe_rental_mode_switch(unit, new_mode)
    if "error" in preview:
        raise RentalModeError(preview["error"])
    if preview["blocked_by"]:
        names = ", ".join(
            f"{b['lease_number']} ({b['status']})" for b in preview["blocked_by"]
        )
        raise RentalModeError(
            f"{unit.name} has live leases and cannot be restructured: {names}. "
            "End or complete them first."
        )

    new_mode = preview["to_mode"]

    # Park the outgoing offerings — never delete.
    _unit_listings(unit).filter(is_active_offering=True).update(
        is_active_offering=False
    )
    # Bring back whatever this unit used to offer in the mode it is entering.
    wanted_category = (
        Property.PropertyCategory.COMPLETE_UNIT
        if new_mode == PropertyUnit.RentalMode.WHOLE_UNIT
        else Property.PropertyCategory.ROOM
    )
    reactivated = _unit_listings(unit).filter(
        is_active_offering=False, property_category=wanted_category
    )
    reactivated_names = [p.name for p in reactivated]
    reactivated.update(is_active_offering=True)

    unit.rental_mode = new_mode
    unit.save(update_fields=["rental_mode", "updated_at"])

    return {
        "ok": True,
        "unit": unit.name,
        "rental_mode": new_mode,
        "parked": preview["will_park"],
        "reactivated": reactivated_names,
        "needs_new_listing": not reactivated_names,
    }
