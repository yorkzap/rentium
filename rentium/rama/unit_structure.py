"""Unit-aware structure tools — how RAMA describes a building.

The old create_house_layout hard-coded the bug the domain model has now
dropped: every bedroom a landlord described became a rentable ROOM listing,
and every floor name became a PropertyGroup. So "McCaughey Main Floor has
three bedrooms, we rent it as one place" produced three separate room listings
and no way to say they were one home.

The rule these tools encode:

    A bedroom described inside a floor is INTERNAL LAYOUT, not an offering.

What is offered is a separate decision — the unit's rental_mode — and when the
landlord hasn't made it clear, we ask exactly one question instead of guessing
or looping.

Three operations, each preview-then-confirm through the same machine as every
other write:

    create_property_structure   address -> units -> internal layout
    update_unit_layout          record/replace what is inside one unit
    set_unit_rental_mode        switch whole-unit <-> room-by-room, guarded
"""

from __future__ import annotations

import json

from django.db import transaction

from rentium.properties.models import Property
from rentium.properties.models import PropertyArea
from rentium.properties.models import PropertyHolding
from rentium.properties.models import PropertyUnit
from rentium.properties.models import Province
from rentium.properties.models import normalise_province

from .domain_crud import _confirmed
from .domain_crud import _preview

MAX_UNITS = 12
MAX_SPACES_PER_UNIT = 40

# What a landlord calls a space -> how we store it.
_SPACE_TYPES = {
    "BEDROOM": PropertyArea.AreaType.BEDROOM,
    "BED": PropertyArea.AreaType.BEDROOM,
    "ROOM": PropertyArea.AreaType.BEDROOM,
    "MASTER": PropertyArea.AreaType.BEDROOM,
    "BATHROOM": PropertyArea.AreaType.BATHROOM,
    "BATH": PropertyArea.AreaType.BATHROOM,
    "WASHROOM": PropertyArea.AreaType.BATHROOM,
    "ENSUITE": PropertyArea.AreaType.BATHROOM,
    "KITCHEN": PropertyArea.AreaType.KITCHEN,
    "KITCHENETTE": PropertyArea.AreaType.KITCHEN,
    "LIVING": PropertyArea.AreaType.LIVING_ROOM,
    "LIVING_ROOM": PropertyArea.AreaType.LIVING_ROOM,
    "LOUNGE": PropertyArea.AreaType.LIVING_ROOM,
    "DINING": PropertyArea.AreaType.DINING_ROOM,
    "DINING_ROOM": PropertyArea.AreaType.DINING_ROOM,
    "OFFICE": PropertyArea.AreaType.OFFICE,
    "DEN": PropertyArea.AreaType.OFFICE,
    "LAUNDRY": PropertyArea.AreaType.LAUNDRY,
    "STORAGE": PropertyArea.AreaType.STORAGE,
    "GARAGE": PropertyArea.AreaType.GARAGE,
    "BALCONY": PropertyArea.AreaType.BALCONY,
    "PATIO": PropertyArea.AreaType.BALCONY,
    "YARD": PropertyArea.AreaType.GARDEN,
    "GARDEN": PropertyArea.AreaType.GARDEN,
    "HALLWAY": PropertyArea.AreaType.HALLWAY,
}

_ACCESS = {
    "PRIVATE": PropertyArea.Kind.PRIVATE,
    "COMMON": PropertyArea.Kind.COMMON,
    "SHARED": PropertyArea.Kind.COMMON,
    "EXCLUSIVE": PropertyArea.Kind.EXCLUSIVE,
    "SYSTEM": PropertyArea.Kind.SYSTEM,
}

_UNIT_TYPES = {
    "BASEMENT": PropertyUnit.UnitType.BASEMENT,
    "GARDEN_SUITE": PropertyUnit.UnitType.GARDEN_SUITE,
    "GARDEN": PropertyUnit.UnitType.GARDEN_SUITE,
    "MAIN_FLOOR": PropertyUnit.UnitType.MAIN_FLOOR,
    "MAIN": PropertyUnit.UnitType.MAIN_FLOOR,
    "APARTMENT": PropertyUnit.UnitType.APARTMENT,
    "SUITE": PropertyUnit.UnitType.APARTMENT,
}

_WHOLE_WORDS = (
    "whole", "entire", "together", "as one", "one unit", "full unit",
    "complete unit", "one lease", "single lease", "as a unit", "family",
)
_ROOM_WORDS = (
    "room by room", "room-by-room", "per room", "each room", "separately",
    "individually", "roommate", "by the room", "different tenants",
)


def _norm(value) -> str:
    return str(value or "").strip()


def _infer_unit_type(name: str) -> str:
    key = _norm(name).upper().replace(" ", "_").replace("-", "_")
    for token, code in _UNIT_TYPES.items():
        if token in key:
            return code
    return PropertyUnit.UnitType.OTHER


def _rental_mode_from_text(text: str) -> str | None:
    """Read an explicit intent out of prose. None = the landlord hasn't said.

    Deliberately returns None rather than a default. Guessing here is what
    produced three room listings for a floor that is let as one home, and the
    landlord cannot see that it guessed.
    """
    low = _norm(text).casefold()
    if not low:
        return None
    room = any(w in low for w in _ROOM_WORDS)
    whole = any(w in low for w in _WHOLE_WORDS)
    if room and not whole:
        return PropertyUnit.RentalMode.BY_ROOM
    if whole and not room:
        return PropertyUnit.RentalMode.WHOLE_UNIT
    return None


def _mode_question(unit_names: list[str]) -> dict:
    names = ", ".join(unit_names)
    return {
        "needs_answer": True,
        "question_for_user": (
            f"For {names}: is it let as ONE home on a single lease, or are the "
            "bedrooms let separately to different people on their own leases?"
        ),
        "relay_instruction": (
            "Ask question_for_user verbatim, then STOP and wait. Do NOT guess a "
            "rental mode and do NOT create any listing yet. When they answer, "
            "call this SAME tool again with rental_mode set on each unit "
            "(WHOLE_UNIT or BY_ROOM). Describing bedrooms is NOT an instruction "
            "to create room listings — bedrooms are internal layout."
        ),
    }


def _parse_spaces(raw_spaces, unit_label: str):
    """(spaces, error). Each space: {name, area_type, kind, serves[]}."""
    if raw_spaces in (None, ""):
        return [], None
    if not isinstance(raw_spaces, list):
        return None, f"{unit_label}: spaces must be a list."
    if len(raw_spaces) > MAX_SPACES_PER_UNIT:
        return None, f"{unit_label}: at most {MAX_SPACES_PER_UNIT} spaces."

    out = []
    seen = set()
    for entry in raw_spaces:
        if isinstance(entry, str):
            entry = {"name": entry}
        if not isinstance(entry, dict):
            return None, f"{unit_label}: each space must be a name or an object."
        name = _norm(entry.get("name"))
        if not name:
            return None, f"{unit_label}: every space needs a name."
        key = name.casefold()
        if key in seen:
            return None, f"{unit_label}: duplicate space {name!r}."
        seen.add(key)

        raw_type = _norm(entry.get("type") or entry.get("area_type") or name)
        code = _SPACE_TYPES.get(raw_type.upper().replace(" ", "_").replace("-", "_"))
        if code is None:
            # Fall back to reading the type out of the name ("Master Bedroom").
            upper = name.upper()
            code = next(
                (v for k, v in _SPACE_TYPES.items() if k in upper),
                PropertyArea.AreaType.OTHER,
            )

        raw_access = _norm(entry.get("access") or entry.get("kind"))
        kind = _ACCESS.get(raw_access.upper())
        if kind is None:
            # A bedroom is somebody's own space; anything else defaults to
            # shared by the household.
            kind = (
                PropertyArea.Kind.PRIVATE
                if code == PropertyArea.AreaType.BEDROOM
                else PropertyArea.Kind.COMMON
            )

        serves = entry.get("serves") or []
        if isinstance(serves, str):
            serves = [s.strip() for s in serves.split(",") if s.strip()]
        if not isinstance(serves, list):
            return None, f"{unit_label}: 'serves' must be a list of space names."

        out.append(
            {
                "name": name,
                "area_type": code,
                "kind": kind,
                "serves": [_norm(s) for s in serves if _norm(s)],
                "shared_with_landlord": bool(entry.get("shared_with_landlord")),
            }
        )
    return out, None


def _parse_units(units_json: str):
    """(units, error). Validates shape only — no writes, no guessing."""
    try:
        payload = json.loads(units_json or "[]")
    except (TypeError, ValueError) as exc:
        return None, f"units_json is not valid JSON: {exc}"
    if isinstance(payload, dict):
        payload = payload.get("units", [])
    if not isinstance(payload, list) or not payload:
        return None, "units_json must be a non-empty list of units."
    if len(payload) > MAX_UNITS:
        return None, f"At most {MAX_UNITS} units at once."

    units = []
    seen = set()
    for raw in payload:
        if isinstance(raw, str):
            raw = {"name": raw}
        if not isinstance(raw, dict):
            return None, "each unit must be a name or an object."
        name = _norm(raw.get("name"))
        if not name:
            return None, "every unit needs a name (e.g. 'Main Floor')."
        key = name.casefold()
        if key in seen:
            return None, f"duplicate unit {name!r}."
        seen.add(key)

        spaces, err = _parse_spaces(raw.get("spaces"), name)
        if err:
            return None, err

        mode = _norm(raw.get("rental_mode")).upper().replace(" ", "_").replace("-", "_")
        if mode in ("BY_ROOM", "ROOMS", "ROOM_BY_ROOM"):
            mode = PropertyUnit.RentalMode.BY_ROOM
        elif mode in ("WHOLE_UNIT", "WHOLE", "UNIT", "ENTIRE"):
            mode = PropertyUnit.RentalMode.WHOLE_UNIT
        elif mode:
            return None, (
                f"{name}: rental_mode must be WHOLE_UNIT or BY_ROOM (got {mode!r})."
            )
        else:
            mode = _rental_mode_from_text(raw.get("note") or raw.get("how_rented"))

        units.append(
            {
                "name": name,
                "unit_type": _norm(raw.get("unit_type")).upper()
                or _infer_unit_type(name),
                "rental_mode": mode,
                "spaces": spaces,
                "listing_name": _norm(raw.get("listing_name")),
                "missing": _norm(raw.get("missing") or raw.get("unknown")),
            }
        )
    return units, None


def _bedroom_count(spaces) -> int | None:
    n = sum(1 for s in spaces if s["area_type"] == PropertyArea.AreaType.BEDROOM)
    return n or None


def _bathroom_count(spaces) -> int | None:
    n = sum(1 for s in spaces if s["area_type"] == PropertyArea.AreaType.BATHROOM)
    return n or None


def _describe(unit) -> dict:
    beds = _bedroom_count(unit["spaces"])
    baths = _bathroom_count(unit["spaces"])
    complete = bool(beds and baths) and not unit["missing"]
    return {
        "unit": unit["name"],
        "rented": unit["rental_mode"],
        "bedrooms": beds,
        "bathrooms": baths,
        "spaces": [s["name"] for s in unit["spaces"]] or None,
        "offering": (
            unit["listing_name"] or unit["name"]
            if unit["rental_mode"] == PropertyUnit.RentalMode.WHOLE_UNIT
            else f"{beds or 'the'} room listing(s) — one per bedroom"
        ),
        "layout_complete": complete,
        "not_recorded": unit["missing"] or (
            "" if complete else "bathroom/bedroom details not fully recorded"
        ),
    }


def create_property_structure(
    landlord,
    *,
    holding_name: str,
    address: str,
    city: str = "",
    province: str = "",
    units_json: str,
    confirm: str = "",
) -> dict:
    """Record a building as UNITS (floors/suites) with their internal layout.
    Bedrooms described inside a unit are internal layout, NOT rentable listings
    — only set rental_mode=BY_ROOM if the landlord rents the bedrooms out
    separately to different people. units_json is a list like
    [{"name":"Main Floor","rental_mode":"WHOLE_UNIT","spaces":[
    {"name":"Master Bedroom","type":"BEDROOM"},{"name":"Ensuite",
    "type":"BATHROOM","serves":["Master Bedroom"]},{"name":"Kitchen"}]}].
    Omit rental_mode when the landlord hasn't said and the tool will ask once.
    Previews first; call again with confirm=yes to apply."""
    holding_name = _norm(holding_name)
    address = _norm(address)
    if not holding_name:
        return {"error": "holding_name is required."}
    if not address:
        return {"error": "address is required."}

    units, err = _parse_units(units_json)
    if err:
        return {"error": err}

    # One question, asked once, for every unit whose mode is still unknown.
    undecided = [u["name"] for u in units if u["rental_mode"] is None]
    if undecided:
        return _mode_question(undecided)

    prov = normalise_province(province) if province else ""
    if province and not prov:
        return {
            "error": f"province {province!r} isn't a Canadian province.",
            "valid": [label for _c, label in Province.choices],
        }

    preview = {
        "holding": holding_name,
        "address": address,
        "city": _norm(city),
        "province": prov,
        "units": [_describe(u) for u in units],
    }
    incomplete = [u["unit"] for u in preview["units"] if not u["layout_complete"]]
    if incomplete:
        preview["flagged_incomplete"] = incomplete
        preview["note"] = (
            "These are created and usable; they are flagged so the missing "
            "details can be filled in later. Nothing is invented."
        )

    if not _confirmed(confirm):
        return _preview(
            "create_property_structure",
            preview,
            "Creates the address, its units, and each unit's internal layout. "
            "Bedrooms become layout, not separate listings, unless a unit is "
            "marked BY_ROOM.",
        )

    return _execute(landlord, holding_name, address, city, prov, units)


@transaction.atomic
def _execute(landlord, holding_name, address, city, prov, units) -> dict:
    holding, _created = PropertyHolding.objects.get_or_create(
        landlord=landlord,
        name=holding_name[:100],
        defaults={"address": address[:255], "city": _norm(city)[:100]},
    )

    created_units = []
    for spec in units:
        unit, _made = PropertyUnit.objects.get_or_create(
            holding=holding,
            name=spec["name"][:100],
            defaults={
                "landlord": landlord,
                "unit_type": spec["unit_type"]
                if spec["unit_type"] in PropertyUnit.UnitType.values
                else PropertyUnit.UnitType.OTHER,
                "rental_mode": spec["rental_mode"],
            },
        )
        beds = _bedroom_count(spec["spaces"])
        baths = _bathroom_count(spec["spaces"])
        unit.rental_mode = spec["rental_mode"]
        unit.layout_complete = bool(beds and baths) and not spec["missing"]
        unit.missing_layout_notes = spec["missing"]
        unit.save(
            update_fields=[
                "rental_mode", "layout_complete", "missing_layout_notes", "updated_at"
            ]
        )

        _write_spaces(unit, spec["spaces"])
        _ensure_offerings(landlord, holding, unit, spec, address, city, prov, beds, baths)
        created_units.append(_describe(spec))

    return {
        "created": True,
        "holding": holding.name,
        "units": created_units,
        "note": (
            "Bedrooms were recorded as internal layout. Only units marked "
            "BY_ROOM have per-room listings."
        ),
    }


def _write_spaces(unit, spaces):
    by_name = {}
    for space in spaces:
        area, _ = PropertyArea.objects.update_or_create(
            unit=unit,
            name=space["name"][:100],
            defaults={
                "area_type": space["area_type"],
                "kind": space["kind"],
                "shared_with_landlord": space["shared_with_landlord"],
                # Recorded because the landlord said so — never scaffolding.
                "is_seeded_default": False,
            },
        )
        by_name[space["name"].casefold()] = area
    for space in spaces:
        if not space["serves"]:
            continue
        area = by_name[space["name"].casefold()]
        targets = [
            by_name[s.casefold()] for s in space["serves"] if s.casefold() in by_name
        ]
        area.serves_areas.set(targets)


def _ensure_offerings(landlord, holding, unit, spec, address, city, prov, beds, baths):
    """One complete-unit listing, or one listing per bedroom — never both."""
    if unit.rental_mode == PropertyUnit.RentalMode.WHOLE_UNIT:
        name = spec["listing_name"] or f"{holding.name} {unit.name}".strip()
        listing, made = Property.objects.get_or_create(
            landlord=landlord,
            unit=unit,
            property_category=Property.PropertyCategory.COMPLETE_UNIT,
            defaults={
                "holding": holding,
                "name": name[:255],
                "address": address[:255],
                "city": _norm(city)[:100],
                "province": prov,
                "unit_type": unit.unit_type or Property.UnitType.OTHER,
                "bedrooms": beds,
                "bathrooms": baths,
                "is_publicly_visible": False,
            },
        )
        if not made:
            listing.bedrooms = beds
            listing.bathrooms = baths
            listing.is_active_offering = True
            listing.save(
                update_fields=[
                    "bedrooms", "bathrooms", "is_active_offering", "updated_at"
                ]
            )
        Property.objects.filter(
            unit=unit, property_category=Property.PropertyCategory.ROOM
        ).update(is_active_offering=False)
        return

    # BY_ROOM: one listing per recorded bedroom.
    bedrooms = [
        s for s in spec["spaces"] if s["area_type"] == PropertyArea.AreaType.BEDROOM
    ]
    for bedroom in bedrooms:
        Property.objects.get_or_create(
            landlord=landlord,
            unit=unit,
            name=bedroom["name"][:255],
            defaults={
                "holding": holding,
                "property_category": Property.PropertyCategory.ROOM,
                "room_type": Property.RoomType.PRIVATE,
                "address": address[:255],
                "city": _norm(city)[:100],
                "province": prov,
                "is_publicly_visible": False,
            },
        )
    Property.objects.filter(
        unit=unit, property_category=Property.PropertyCategory.COMPLETE_UNIT
    ).update(is_active_offering=False)


# ------------------------------------------------------------------ layout
def _resolve_unit(landlord, unit_name: str):
    from django.db.models import Q

    query = _norm(unit_name)
    if not query:
        return None, {"error": "unit_name is required."}
    qs = PropertyUnit.objects.filter(landlord=landlord).select_related("holding")
    matches = list(
        qs.filter(Q(name__icontains=query) | Q(holding__name__icontains=query))[:6]
    )
    exact = [u for u in matches if u.name.casefold() == query.casefold()]
    if len(exact) == 1:
        return exact[0], None
    if not matches:
        return None, {"error": f"No unit matching {unit_name!r}."}
    if len(matches) > 1:
        return None, {
            "error": f"Several units match {unit_name!r} — which one?",
            "candidates": [f"{u.name} ({u.holding.name})" for u in matches],
        }
    return matches[0], None


def update_unit_layout(
    landlord,
    *,
    unit_name: str,
    spaces_json: str = "",
    layout_complete: str = "",
    missing: str = "",
    confirm: str = "",
) -> dict:
    """Record what is INSIDE one unit — its bedrooms, bathrooms, kitchen and
    other spaces. This never creates listings: describing a bedroom does not
    put it on the market. spaces_json is a list like [{"name":"Master
    Bedroom","type":"BEDROOM"},{"name":"Ensuite","type":"BATHROOM",
    "serves":["Master Bedroom"]}]. Use `missing` to say what is still unknown
    rather than guessing it. Previews first; confirm=yes to apply."""
    unit, err = _resolve_unit(landlord, unit_name)
    if err:
        return err

    spaces, perr = _parse_spaces(json.loads(spaces_json or "[]"), unit.name) if (
        spaces_json or ""
    ).strip() else ([], None)
    if perr:
        return {"error": perr}

    beds, baths = _bedroom_count(spaces), _bathroom_count(spaces)
    complete = _norm(layout_complete).casefold() in ("1", "true", "yes", "y")
    preview = {
        "unit": f"{unit.name} ({unit.holding.name})",
        "spaces": [s["name"] for s in spaces] or None,
        "bedrooms": beds,
        "bathrooms": baths,
        "layout_complete": complete or bool(beds and baths and not _norm(missing)),
        "still_unknown": _norm(missing) or None,
        "note": "Recording layout does not create or change any listing.",
    }
    if not _confirmed(confirm):
        return _preview(
            "update_unit_layout", preview, "Records this unit's internal spaces."
        )

    with transaction.atomic():
        _write_spaces(unit, spaces)
        unit.layout_complete = preview["layout_complete"]
        unit.missing_layout_notes = _norm(missing)
        unit.save(
            update_fields=["layout_complete", "missing_layout_notes", "updated_at"]
        )
    return {"updated": True, **preview}


def set_unit_rental_mode(
    landlord, *, unit_name: str, rental_mode: str, confirm: str = ""
) -> dict:
    """Switch a unit between being let as ONE home (WHOLE_UNIT) and let room by
    room (BY_ROOM). Nothing is deleted — listings for the other mode are parked
    and come back if you switch again. Refused while any draft, pending or
    active lease exists in the unit. Previews first; confirm=yes to apply."""
    from rentium.properties.services import RentalModeError
    from rentium.properties.services import describe_rental_mode_switch
    from rentium.properties.services import set_rental_mode

    unit, err = _resolve_unit(landlord, unit_name)
    if err:
        return err

    mode = _norm(rental_mode).upper().replace(" ", "_").replace("-", "_")
    if mode in ("ROOMS", "ROOM_BY_ROOM", "BY_ROOM"):
        mode = PropertyUnit.RentalMode.BY_ROOM
    elif mode in ("WHOLE", "UNIT", "ENTIRE", "WHOLE_UNIT"):
        mode = PropertyUnit.RentalMode.WHOLE_UNIT

    preview = describe_rental_mode_switch(unit, mode)
    if "error" in preview:
        return {"error": preview["error"]}
    if preview["blocked_by"]:
        return {
            "error": (
                f"{unit.name} has live leases, so how it is rented cannot be "
                "changed yet."
            ),
            "blocked_by": preview["blocked_by"],
            "relay_instruction": (
                "Tell the landlord which leases block this and stop. Do not "
                "try to work around it by creating or deleting listings."
            ),
        }
    if not _confirmed(confirm):
        return _preview(
            "set_unit_rental_mode",
            preview,
            "Parks the current listings and brings back the other mode's. "
            "Nothing is deleted.",
        )
    try:
        return set_rental_mode(unit, mode)
    except RentalModeError as exc:
        return {"error": exc.messages[0] if exc.messages else str(exc)}
