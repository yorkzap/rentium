"""Atomic creation of a physical house and its room-level layout.

This service exists for instructions that describe a hierarchy in one breath:

    house -> property groups -> rooms -> private/subset/common areas

Breaking that request into independent model-authored calls is unsafe.  A
single preview and transaction keeps the hierarchy internally consistent and
makes one landlord confirmation mean exactly one saved operation.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass

from django.core.exceptions import ValidationError
from django.db import transaction

from rentium.properties.models import Property
from rentium.properties.models import PropertyArea
from rentium.properties.models import PropertyGroup
from rentium.properties.models import PropertyHolding
from rentium.properties.models import Province
from rentium.properties.models import normalise_province
from rentium.properties.services import create_group_common_area
from rentium.properties.services import listing_name_conflicts

MAX_GROUPS = 12
MAX_ROOMS = 40
MAX_AREAS = 80
MIN_SHARED_ROOMS = 2


@dataclass(frozen=True)
class ParsedLayout:
    groups: list[dict]
    room_count: int
    area_count: int


def _confirmed(value: str) -> bool:
    return str(value or "").strip().casefold() in {
        "1",
        "true",
        "yes",
        "y",
        "confirm",
        "confirmed",
    }


def _normalise(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", str(value or "").casefold())


def _classification(value: str) -> tuple[bool | None, str | None]:
    cleaned = str(value or "").strip().casefold()
    if not cleaned:
        return None, None
    if cleaned in {"1", "true", "yes", "y", "on", "shared"}:
        return True, None
    if cleaned in {
        "0",
        "false",
        "no",
        "n",
        "off",
        "tenant only",
        "tenants only",
    }:
        return False, None
    return None, "shared_with_landlord must be yes or no."


def _area_type(value: str) -> str:
    raw = str(value or "").strip()
    if not raw:
        return ""
    code = raw.upper().replace(" ", "_").replace("-", "_")
    if code in PropertyArea.AreaType.values:
        return code
    aliases = {
        "BATH": PropertyArea.AreaType.BATHROOM,
        "WASHROOM": PropertyArea.AreaType.BATHROOM,
        "ENSUITE": PropertyArea.AreaType.BATHROOM,
        "LIVING": PropertyArea.AreaType.LIVING_ROOM,
        "DEN": PropertyArea.AreaType.OFFICE,
        "KITCHENETTE": PropertyArea.AreaType.KITCHEN,
    }
    return aliases.get(code, "")


def _parse_layout(  # noqa: C901, PLR0911, PLR0912 - explicit validation errors
    layout_json: str,
) -> tuple[ParsedLayout | None, str | None]:
    try:
        payload = json.loads(layout_json or "{}")
    except (TypeError, ValueError) as exc:
        return None, f"layout_json is not valid JSON: {exc}"
    if not isinstance(payload, dict) or not isinstance(payload.get("groups"), list):
        return None, "layout_json must contain a groups list."
    if not payload["groups"]:
        return None, "At least one property group is required."
    if len(payload["groups"]) > MAX_GROUPS:
        return None, f"A house layout can contain at most {MAX_GROUPS} groups."

    group_names: set[str] = set()
    all_room_names: set[str] = set()
    groups: list[dict] = []
    area_count = 0
    for group_index, raw_group in enumerate(payload["groups"], start=1):
        if not isinstance(raw_group, dict):
            return None, f"Group {group_index} must be an object."
        group_name = str(raw_group.get("name") or "").strip()
        if not group_name:
            return None, f"Group {group_index} needs a name."
        group_key = _normalise(group_name)
        if group_key in group_names:
            return None, f"Property group {group_name!r} is repeated."
        group_names.add(group_key)

        raw_rooms = raw_group.get("rooms") or []
        if not isinstance(raw_rooms, list):
            return None, f"Rooms for {group_name} must be a list."
        rooms: list[dict] = []
        group_room_names: set[str] = set()
        for room_index, raw_room in enumerate(raw_rooms, start=1):
            if not isinstance(raw_room, dict):
                return None, f"Room {room_index} in {group_name} must be an object."
            room_name = str(raw_room.get("name") or "").strip()
            if not room_name:
                return None, f"Room {room_index} in {group_name} needs a name."
            room_key = _normalise(room_name)
            if room_key in all_room_names:
                return None, f"Room name {room_name!r} is repeated."
            all_room_names.add(room_key)
            group_room_names.add(room_key)
            private_areas: list[str] = []
            for raw_area in raw_room.get("private_areas") or []:
                area_type = _area_type(raw_area)
                if not area_type:
                    return None, (
                        f"Unrecognized private area {raw_area!r} for {room_name}."
                    )
                if area_type not in private_areas:
                    private_areas.append(area_type)
            area_count += len(private_areas)
            rooms.append({"name": room_name, "private_areas": private_areas})

        shared_areas: list[dict] = []
        for raw_area in raw_group.get("shared_areas") or []:
            if not isinstance(raw_area, dict):
                return None, f"A shared area in {group_name} is not an object."
            area_type = _area_type(
                raw_area.get("area_type") or raw_area.get("type") or "",
            )
            if not area_type:
                return None, f"Unrecognized shared area in {group_name}."
            target_names = [
                str(name or "").strip() for name in (raw_area.get("rooms") or [])
            ]
            target_keys = {_normalise(name) for name in target_names if name}
            if len(target_keys) < MIN_SHARED_ROOMS:
                return None, (
                    f"The {PropertyArea.AreaType(area_type).label} in {group_name} "
                    "must name at least two rooms that share it."
                )
            unknown = target_keys - group_room_names
            if unknown:
                return None, (
                    f"A shared area in {group_name} refers to a room outside "
                    "that group."
                )
            shared_areas.append(
                {
                    "area_type": area_type,
                    "room_keys": sorted(target_keys),
                    "description": str(raw_area.get("description") or "").strip(),
                },
            )
            area_count += 1
        groups.append(
            {
                "name": group_name,
                "description": str(raw_group.get("description") or "").strip(),
                "rooms": rooms,
                "shared_areas": shared_areas,
            },
        )

    if len(all_room_names) > MAX_ROOMS:
        return None, f"A house layout can contain at most {MAX_ROOMS} rooms."
    if area_count > MAX_AREAS:
        return None, f"A house layout can contain at most {MAX_AREAS} areas."
    return ParsedLayout(groups, len(all_room_names), area_count), None


def _holding_match(landlord, holding_name: str, address: str):
    candidates = list(
        PropertyHolding.objects.filter(landlord=landlord).order_by("created_at"),
    )
    name_key = _normalise(holding_name)
    address_key = _normalise(address)
    exact_name = [item for item in candidates if _normalise(item.name) == name_key]
    exact_address = [
        item
        for item in candidates
        if item.address and _normalise(item.address) == address_key
    ]
    matches = {item.pk: item for item in [*exact_name, *exact_address]}
    if len(matches) > 1:
        return None, (
            "The holding name and address point to different existing holdings. "
            "Correct those records before creating this layout."
        )
    return (next(iter(matches.values())) if matches else None), None


def _existing_room(landlord, room_name: str):
    conflicts = listing_name_conflicts(landlord, room_name)
    exact = [item for item in conflicts if item["match"] == "exact"]
    if len(exact) > 1:
        return None, conflicts, (
            f"More than one listing matches {room_name!r}; use distinct room names."
        )
    if not exact:
        return None, conflicts, None
    room = Property.objects.filter(landlord=landlord, pk=exact[0]["id"]).first()
    return room, conflicts, None


def _validation_snapshot(  # noqa: C901, PLR0911, PLR0913
    landlord,
    *,
    holding_name: str,
    address: str,
    city: str,
    province: str,
    layout: ParsedLayout,
) -> tuple[dict | None, str | None]:
    holding, holding_error = _holding_match(landlord, holding_name, address)
    if holding_error:
        return None, holding_error
    if holding is not None:
        if holding.address and _normalise(holding.address) != _normalise(address):
            return None, (
                f"{holding.name} is recorded at {holding.address}, not {address}."
            )
        if holding.city and _normalise(holding.city) != _normalise(city):
            return None, f"{holding.name} is recorded in {holding.city}, not {city}."

    group_rows: list[dict] = []
    room_rows: list[dict] = []
    near_duplicates: list[dict] = []
    for group_spec in layout.groups:
        group = PropertyGroup.objects.filter(
            landlord=landlord,
            name__iexact=group_spec["name"],
        ).first()
        if group is not None:
            members = group.grouped_properties.select_related("holding")
            bad_member = next(
                (
                    member
                    for member in members
                    if _normalise(member.address) != _normalise(address)
                    or (
                        member.holding_id
                        and holding is not None
                        and member.holding_id != holding.pk
                    )
                ),
                None,
            )
            if bad_member is not None:
                return None, (
                    f"{group.name} already contains {bad_member.name} at "
                    f"{bad_member.address}; it cannot be reused for {address}."
                )
        group_rows.append(
            {
                "name": group_spec["name"],
                "action": "reuse" if group else "create",
                "room_count": len(group_spec["rooms"]),
            },
        )
        for room_spec in group_spec["rooms"]:
            room, conflicts, room_error = _existing_room(landlord, room_spec["name"])
            if room_error:
                return None, room_error
            if room is not None:
                same_group = room.group and (
                    _normalise(room.group.name) == _normalise(group_spec["name"])
                )
                same_holding = (
                    (holding is not None and room.holding_id == holding.pk)
                    or _normalise(room.address) == _normalise(address)
                )
                if not same_group or not same_holding:
                    return None, (
                        f"A listing named {room.name!r} already exists in another "
                        "group or holding. Choose a distinct room name."
                    )
            near_duplicates.extend(
                item for item in conflicts if item["match"] == "near"
            )
            room_rows.append(
                {
                    "name": room_spec["name"],
                    "group": group_spec["name"],
                    "action": "reuse" if room else "create",
                    "private_areas": [
                        str(PropertyArea.AreaType(code).label)
                        for code in room_spec["private_areas"]
                    ],
                },
            )
    return {
        "holding": holding,
        "holding_action": "reuse" if holding else "create",
        "groups": group_rows,
        "rooms": room_rows,
        "near_duplicates": near_duplicates,
        "city": city,
        "province": province,
    }, None


def _find_subset_area(group, area_type: str, room_ids: set[int]):
    candidates = (
        PropertyArea.objects.select_for_update()
        .filter(
            property__group=group,
            area_type=area_type,
            is_group_common=False,
        )
        .prefetch_related("shared_by")
    )
    for area in candidates:
        if set(area.shared_by.values_list("pk", flat=True)) == room_ids:
            return area
    return None


def _shared_area_preview(
    layout: ParsedLayout,
    *,
    classification: bool,
) -> list[dict]:
    rows: list[dict] = []
    for group_spec in layout.groups:
        room_labels = {
            _normalise(room["name"]): room["name"] for room in group_spec["rooms"]
        }
        rows.extend(
            {
                "name": str(PropertyArea.AreaType(area["area_type"]).label),
                "group": group_spec["name"],
                "rooms": [room_labels[key] for key in area["room_keys"]],
                "shared_with_landlord": classification,
            }
            for area in group_spec["shared_areas"]
        )
    return rows


def _execute_layout(  # noqa: C901, PLR0912, PLR0915 - one atomic domain write
    landlord,
    request: dict,
    layout: ParsedLayout,
    *,
    classification: bool,
) -> tuple[PropertyHolding, dict, dict]:
    """Execute an already-validated hierarchy inside one database transaction."""
    holding_name = request["holding_name"]
    address = request["address"]
    city = request["city"]
    province = request["province"]
    created = {"holding": False, "groups": 0, "rooms": 0, "areas": 0}
    reused = {"holding": False, "groups": 0, "rooms": 0, "areas": 0}

    with transaction.atomic():
        locked_holding, holding_error = _holding_match(
            landlord,
            holding_name,
            address,
        )
        if holding_error:
            raise ValidationError(holding_error)
        if locked_holding is None:
            locked_holding = PropertyHolding.objects.create(
                landlord=landlord,
                name=holding_name[:100],
                kind=PropertyHolding.Kind.HOUSE,
                address=address[:255],
                city=city[:100],
            )
            created["holding"] = True
        else:
            locked_holding = PropertyHolding.objects.select_for_update().get(
                pk=locked_holding.pk,
            )
            reused["holding"] = True

        groups_by_key: dict[str, PropertyGroup] = {}
        rooms_by_key: dict[str, Property] = {}
        for group_spec in layout.groups:
            group = (
                PropertyGroup.objects.select_for_update()
                .filter(
                    landlord=landlord,
                    name__iexact=group_spec["name"],
                )
                .first()
            )
            if group is None:
                group = PropertyGroup.objects.create(
                    landlord=landlord,
                    name=group_spec["name"][:100],
                    description=group_spec["description"][:2000],
                )
                created["groups"] += 1
            else:
                reused["groups"] += 1
            groups_by_key[_normalise(group_spec["name"])] = group

            for room_spec in group_spec["rooms"]:
                room, _conflicts, room_error = _existing_room(
                    landlord,
                    room_spec["name"],
                )
                if room_error:
                    raise ValidationError(room_error)
                if room is None:
                    room = Property(
                        landlord=landlord,
                        holding=locked_holding,
                        group=group,
                        name=room_spec["name"][:255],
                        address=address[:255],
                        city=city[:100],
                        province=province,
                        country="Canada",
                        address_verified=False,
                        status=Property.PropertyStatus.AVAILABLE,
                        property_category=Property.PropertyCategory.ROOM,
                        room_type=Property.RoomType.PRIVATE,
                    )
                    room.full_clean()
                    room.save()
                    created["rooms"] += 1
                else:
                    room = Property.objects.select_for_update().get(pk=room.pk)
                    if (
                        room.group_id != group.pk
                        or room.holding_id != locked_holding.pk
                        or _normalise(room.address) != _normalise(address)
                    ):
                        message = (
                            f"{room.name} changed after the preview; nothing was saved."
                        )
                        raise ValidationError(message)
                    reused["rooms"] += 1
                rooms_by_key[_normalise(room_spec["name"])] = room

                for area_type in room_spec["private_areas"]:
                    private_area = None
                    candidates = (
                        PropertyArea.objects.select_for_update()
                        .filter(
                            property=room,
                            area_type=area_type,
                            is_group_common=False,
                        )
                        .prefetch_related("shared_by")
                    )
                    for candidate in candidates:
                        member_ids = set(
                            candidate.shared_by.values_list("pk", flat=True),
                        )
                        if not member_ids or member_ids == {room.pk}:
                            private_area = candidate
                            break
                    if private_area is None:
                        private_area = PropertyArea.objects.create(
                            property=room,
                            area_type=area_type,
                            count=1,
                            description=f"Private area for {room.name}.",
                            shared_with_landlord=False,
                            is_group_common=False,
                        )
                        private_area.shared_by.set([room.pk])
                        created["areas"] += 1
                    else:
                        private_area.shared_by.set([room.pk])
                        reused["areas"] += 1

        for group_spec in layout.groups:
            group = groups_by_key[_normalise(group_spec["name"])]
            all_group_room_ids = set(
                group.grouped_properties.filter(
                    property_category=Property.PropertyCategory.ROOM,
                ).values_list("pk", flat=True),
            )
            for area_spec in group_spec["shared_areas"]:
                target_rooms = [
                    rooms_by_key[room_key] for room_key in area_spec["room_keys"]
                ]
                target_ids = {room.pk for room in target_rooms}
                if target_ids == all_group_room_ids:
                    _area, was_created = create_group_common_area(
                        group,
                        area_type=area_spec["area_type"],
                        count=1,
                        description=area_spec["description"],
                        shared_with_landlord=classification,
                    )
                else:
                    area = _find_subset_area(
                        group,
                        area_spec["area_type"],
                        target_ids,
                    )
                    was_created = area is None
                    if area is None:
                        area = PropertyArea.objects.create(
                            property=target_rooms[0],
                            area_type=area_spec["area_type"],
                            count=1,
                            description=area_spec["description"],
                            shared_with_landlord=classification,
                            is_group_common=False,
                        )
                    else:
                        area.description = (
                            area_spec["description"] or area.description
                        )
                        area.shared_with_landlord = classification
                        area.save(
                            update_fields=[
                                "description",
                                "shared_with_landlord",
                                "updated_at",
                            ],
                        )
                    area.shared_by.set(target_ids)
                if was_created:
                    created["areas"] += 1
                else:
                    reused["areas"] += 1
    return locked_holding, created, reused


def create_house_layout(  # noqa: C901, PLR0911, PLR0912, PLR0913
    landlord,
    *,
    holding_name: str,
    address: str,
    city: str = "",
    province: str = "",
    layout_json: str,
    shared_with_landlord: str = "",
    confirm: str = "",
) -> dict:
    """Preview or atomically create a house, groups, rooms, and area access."""
    holding_name = str(holding_name or "").strip()
    address = str(address or "").strip()
    city = str(city or "").strip()
    raw_province = str(province or "").strip()
    if not holding_name:
        return {"error": "holding_name is required."}
    if not address:
        return {"error": "address is required."}

    parsed, parse_error = _parse_layout(layout_json)
    if parse_error:
        return {"error": parse_error}
    assert parsed is not None
    classification, classification_error = _classification(shared_with_landlord)
    if classification_error:
        return {"error": classification_error}

    missing: list[str] = []
    if not city:
        missing.append("city")
    canonical_province = normalise_province(raw_province)
    if not raw_province:
        missing.append("province")
    elif not canonical_province or canonical_province not in Province.values:
        return {
            "error": (
                f"Invalid province {raw_province!r}. Use a Canadian province "
                "name or two-letter code."
            ),
        }
    if parsed.area_count and any(
        group["shared_areas"] for group in parsed.groups
    ) and classification is None:
        missing.append("shared_with_landlord")
    if missing:
        questions: list[str] = []
        if "city" in missing or "province" in missing:
            questions.append(f"What city and province is {address} in?")
        if "shared_with_landlord" in missing:
            questions.append(
                "Does the landlord or an immediate relative also use the shared "
                "kitchen, living room, or shared washroom? Answer yes or no.",
            )
        empty_groups = [
            group["name"] for group in parsed.groups if not group["rooms"]
        ]
        note = ""
        if empty_groups:
            note = (
                " I can create "
                + ", ".join(empty_groups)
                + " as an empty group now and add its rooms later."
            )
        return {
            "needs_input": True,
            "missing_fields": missing,
            "question_for_user": (
                "I understand the house layout and kept it together as one draft."
                + note
                + "\n"
                + "\n".join(f"• {question}" for question in questions)
            ),
            "draft": {
                "holding_name": holding_name,
                "address": address,
                "groups": [group["name"] for group in parsed.groups],
                "rooms": parsed.room_count,
            },
        }

    snapshot, snapshot_error = _validation_snapshot(
        landlord,
        holding_name=holding_name,
        address=address,
        city=city,
        province=canonical_province,
        layout=parsed,
    )
    if snapshot_error:
        return {"error": snapshot_error}
    assert snapshot is not None

    preview = {
        "holding": {
            "name": holding_name,
            "address": address,
            "city": city,
            "province": canonical_province,
            "action": snapshot["holding_action"],
        },
        "groups": snapshot["groups"],
        "rooms": snapshot["rooms"],
        "shared_areas": _shared_area_preview(
            parsed,
            classification=bool(classification),
        ),
        "near_duplicate_names": snapshot["near_duplicates"],
        "atomic": True,
    }
    if not _confirmed(confirm):
        return {
            "action": "create_house_layout",
            "preview": preview,
            "needs_confirm": True,
            "instruction": (
                "Show the complete hierarchy preview. On approval, call this "
                "tool again with identical arguments and confirm=yes."
            ),
        }

    request = {
        "holding_name": holding_name,
        "address": address,
        "city": city,
        "province": canonical_province,
    }
    try:
        locked_holding, created, reused = _execute_layout(
            landlord,
            request,
            parsed,
            classification=bool(classification),
        )
    except ValidationError as exc:
        messages = getattr(exc, "messages", None) or [str(exc)]
        return {
            "error": (
                "Could not create the house layout; nothing was saved: "
                + " ".join(messages)
            ),
        }
    except Exception as exc:  # noqa: BLE001 - transaction is already rolled back
        return {
            "error": (
                f"Could not create the house layout; nothing was saved: {exc}"
            ),
        }

    return {
        "created": True,
        "holding": {
            "id": str(locked_holding.pk),
            "name": locked_holding.name,
            "address": locked_holding.address,
        },
        "created_counts": created,
        "reused_counts": reused,
        "message": (
            f"Created house layout for {locked_holding.address}: "
            f"{created['groups']} new group(s), {created['rooms']} new room(s), "
            f"and {created['areas']} new area(s)."
        ),
    }
