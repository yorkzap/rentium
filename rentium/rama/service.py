"""
The RAMA turn engine, independent of HTTP.

`run_turn` is the single entry point every surface uses: the web chat views,
the Telegram webhook (Phase 3), scheduled analysis turns (Phase 4), and
role-to-role delegation (`ask_corporal` / `ask_fsa` run sub-turns with
depth=1). A role only changes the system prompt, the tool subset, and the
model config — the deterministic confirm state machine, memory, grounding,
and guardrails are identical for every role.

Doctrine (unchanged): on a BARE yes/no the model is never consulted — the
backend already knows exactly what ran or was cancelled; weak models asked to
"narrate" outcomes invent actions that never happened.
"""

from __future__ import annotations

import json
import re
import uuid
from dataclasses import dataclass, field
from datetime import timedelta

from django.utils import timezone

from .models import RamaAudit, RamaPendingPlan
from .plan_runner import (
    PENDING_PLAN_TTL_SECONDS,
    clear_plan,
    load_fresh_plan,
    plan_brief,
    run_plan,
    save_batch,
    save_plan,
    save_single,
)
from .plan_runner import plan_to_payload
from .providers import ProviderError, Turn, get_provider
from .registry import REGISTRY, execute
from .roles import (
    DELEGATION_TOOL_NAMES,
    ROLE_PROMPTS,
    SUB_TURN_MAX_ROUNDS,
    role_context,
    role_tool_schemas,
)
from .runtime import get_role_config
from .union import live_context

MAX_TOOL_ROUNDS = 20  # multi-step room/lease/invite needs headroom
HISTORY_TURNS = 12
MAX_MESSAGE_CHARS = 6000
# A previewed plan is honored as "the thing the landlord just said yes to" only
# while it's fresh; after this it's stale and the model must re-preview.
PENDING_ACTION_TTL_SECONDS = PENDING_PLAN_TTL_SECONDS

# Bare affirmations that mean "run the action you just previewed". The confirm
# step is executed by the backend from the persisted pending plan, so a weak
# model can never lose the exact tool + args and fall into a re-preview loop.
_AFFIRM_EXACT = {
    "yes", "y", "ye", "yea", "yeah", "yep", "yup", "ya", "sure", "ok", "okay",
    "k", "kk", "confirm", "confirmed", "proceed", "correct", "aye", "affirmative",
    "right", "do it", "go", "go ahead", "go for it", "sounds good", "please do",
    "do that", "yes do it", "do", "make it so", "yes please",
}
_AFFIRM_LEAD = (
    "yes please", "go ahead", "please do", "do it", "confirm", "confirmed",
    "proceed", "correct", "yes", "yeah", "yep", "yup", "sure", "okay", "ok",
)


def _norm_affirm(text: str) -> str:
    return (text or "").strip().lower().strip(" .!,\t\n")


def _is_affirmative(text: str) -> bool:
    """True when the message is a plain 'yes' (optionally with a follow-on
    instruction after a conjunction, e.g. 'yes and delete the other one')."""
    t = _norm_affirm(text)
    if not t:
        return False
    if t in _AFFIRM_EXACT:
        return True
    for lead in _AFFIRM_LEAD:
        if t.startswith(lead + " ") or t.startswith(lead + ",") or t.startswith(lead + "."):
            rest = t[len(lead):].lstrip(" ,.")
            # Require a conjunction / "please" so we don't misread a change of
            # mind ("yes actually cancel that") as confirmation.
            if rest.startswith(("and ", "then ", "also ", "plus ", "please")):
                return True
    return False


# Bare rejections that mean "don't run the plan you previewed". Deterministic,
# like _is_affirmative — the model never decides whether a plan was cancelled.
_NEGATIVE_EXACT = {
    "no", "n", "nope", "nah", "cancel", "cancel that", "cancel it", "stop",
    "abort", "don't", "dont", "do not", "never mind", "nevermind", "forget it",
    "no thanks", "not now", "leave it", "skip it", "don't do it", "dont do it",
}


def _is_negative(text: str) -> bool:
    return _norm_affirm(text) in _NEGATIVE_EXACT


def _recent_confirmed_reply(landlord, conversation_id) -> str:
    """Return the just-completed plan reply for a duplicate bare ``yes``.

    Telegram can deliver a repeated acknowledgement after the plan row has
    already been consumed. Only an audited auto-confirmed tool call qualifies;
    ordinary read-only deterministic replies must not swallow a new message.
    """
    confirmed_call = (
        RamaAudit.objects.filter(
            landlord=landlord,
            conversation_id=conversation_id,
            kind=RamaAudit.Kind.TOOL_CALL,
            content__auto_confirmed=True,
            created_at__gte=timezone.now()
            - timedelta(minutes=2),
        )
        .order_by("-created_at")
        .first()
    )
    if confirmed_call is None:
        return ""
    reply = (
        RamaAudit.objects.filter(
            landlord=landlord,
            conversation_id=conversation_id,
            kind=RamaAudit.Kind.ASSISTANT_MESSAGE,
            created_at__gte=confirmed_call.created_at,
        )
        .order_by("-created_at")
        .first()
    )
    if reply is None:
        return ""
    content = reply.content or {}
    # Only replay the deterministic result emitted by the confirm machine.
    # A later model-authored "preview" is not proof that the preceding Yes was
    # already handled; treating it as such created nested "already applied"
    # replies while leaving the newly described work impossible to confirm.
    if not content.get("confirmation_result"):
        return ""
    text = str(content.get("text") or "")
    if "That confirmation was already applied" in text:
        return ""
    return text


def _write_label(result: dict) -> str:
    for key in ("property", "lease", "group", "work_order"):
        obj = result.get(key)
        if isinstance(obj, dict):
            return str(obj.get("name") or obj.get("lease_number") or obj.get("id") or "").strip()
    return ""


def _write_result_message(tool: str, result: dict, target: str = "") -> str:
    message = str(result.get("message") or result.get("note") or "").strip()
    if message:
        return message
    if tool == "update_property":
        before = str(result.get("previous_name") or target or "").strip()
        prop = result.get("property") or {}
        after = str(prop.get("name") if isinstance(prop, dict) else prop).strip()
        if before and after and before != after:
            return f"Renamed {before} to {after}."
        if after:
            return f"Updated {after}."
    label = _write_label(result) or target
    if result.get("created") and label:
        return f"Created {label}."
    if result.get("updated") and label:
        return f"Updated {label}."
    if result.get("deleted") and label:
        return f"Deleted {label}."
    return f"Completed {tool.replace('_', ' ')}" + (f" for {label}." if label else ".")


def _plan_fallback_reply(progress: dict) -> str:
    """Deterministic per-item report for an executed plan — completed work
    must never look like an error, and never needs a model to describe it."""
    parts: list[str] = []
    for it in progress.get("executed") or []:
        result = it.get("result") or {}
        parts.append(
            _write_result_message(
                str(it.get("tool") or ""),
                result,
                str(it.get("target") or ""),
            )
        )
        if result.get("documents_page"):
            parts.append(f"Open document: {result['documents_page']}")
    for it in progress.get("failed") or []:
        err = (it.get("result") or {}).get("error") or "failed"
        parts.append(f"Failed: {it.get('target') or it.get('tool')} — {err}")
    for it in progress.get("skipped") or []:
        parts.append(f"Skipped: {it.get('target') or it.get('tool')}")
    awaiting = progress.get("awaiting")
    if awaiting:
        parts.append(
            f"Next: {awaiting.get('target') or awaiting.get('tool')} needs its "
            "own confirmation — reply yes to run it, or no to stop here."
        )
    return "\n".join(parts) or "Done."


def _rename_intent(landlord, message: str) -> dict | None:
    match = re.match(
        r"^\s*(?:please\s+)?rename\s+(?:the\s+)?(.+?)\s+to\s+(.+?)\s*[.!]?\s*$",
        message or "",
        flags=re.IGNORECASE,
    )
    if not match:
        return None
    source = match.group(1).strip()
    new_name = match.group(2).strip()
    if not source or not new_name:
        return None
    # This router is deliberately for one unambiguous rename. A compound
    # command belongs to the collected-preview planner; treating its entire
    # tail as the new listing name produced absurd names such as
    # "Room 1 and Room 5 to Room 2, then assign …".
    if re.search(
        r"\b(?:then|assign|move|put)\b|(?:,|\band\b).+\bto\b",
        new_name,
        flags=re.IGNORECASE,
    ):
        return None
    room_label = re.fullmatch(r"room\s+([a-z])", new_name, flags=re.IGNORECASE)
    if room_label:
        new_name = f"Room {room_label.group(1).upper()}"

    from .resolve import resolve_property

    prop, err = resolve_property(landlord, source)
    if err and re.match(r"^room\s+", source, flags=re.IGNORECASE):
        suffix = re.sub(r"^room\s+", "", source, flags=re.IGNORECASE).strip()
        from rentium.properties.models import Property

        candidates = list(
            Property.objects.filter(
                landlord=landlord,
                property_category=Property.PropertyCategory.ROOM,
                name__iendswith=suffix,
            ).order_by("created_at")[:3]
        )
        if len(candidates) == 1:
            prop, err = candidates[0], None
    return {
        "tool": "update_property",
        "arguments": {
            "property_query": str(prop.pk) if prop is not None else source,
            "name": new_name,
        },
    }


def _dashboard_collection_intent(message: str) -> str | None:
    text = " ".join((message or "").casefold().split())
    if "dashboard" not in text or not re.search(
        r"\b(link|open|view|show|go|take|send|where)\b", text
    ):
        return None
    for pattern, collection in (
        (r"\bproperty groups?\b", "property_groups"),
        (r"\bproperties\b|\blistings\b", "properties"),
        (r"\bdocuments?\b", "documents"),
        (r"\bleases?\b", "leases"),
        (r"\bfinances?\b|\bfinancial\b", "finances"),
        (r"\bmaintenance\b|\brepairs?\b", "maintenance"),
        (r"\bsettings?\b", "settings"),
    ):
        if re.search(pattern, text):
            return collection
    return "dashboard"


def _show_all_rooms_intent(message: str) -> bool:
    text = " ".join((message or "").casefold().split())
    return bool(
        re.search(r"\b(show|list|view)\b", text)
        and re.search(r"\b(all|every|my)\b", text)
        and re.search(r"\brooms?\b", text)
    )


def _rooms_reply(result: dict) -> str:
    rooms = result.get("rooms") or []
    if not rooms:
        return "No room listings are recorded."
    parents: dict[tuple[str, str], dict[str, list[dict]]] = {}
    for room in rooms:
        parent = room.get("holding") or room.get("address") or "No holding/address"
        address = room.get("address") or ""
        group = room.get("group") or "Ungrouped rooms"
        parents.setdefault((parent, address), {}).setdefault(group, []).append(room)
    lines = [f"Rooms ({len(rooms)}):"]
    for (parent, address), groups in parents.items():
        heading = parent
        if address and address.casefold() not in parent.casefold():
            heading += f" — {address}"
        lines.append(heading)
        for group, members in groups.items():
            lines.append(f"  {group}")
            for room in members:
                occupancy = room.get("occupancy") or {}
                state = (
                    "occupied today"
                    if occupancy.get("occupied_today")
                    else "vacant today"
                )
                lines.append(f"  • {room.get('name')} — {state}")
    return "\n".join(lines)


def _group_room_intent(landlord, message: str) -> dict | None:
    text = message or ""
    lowered = text.casefold()
    if not re.search(r"\b(create|add|make)\b", lowered):
        return None
    from rentium.properties.models import PropertyGroup
    from rentium.properties.services import parse_common_area_types

    matched_groups = [
        group
        for group in PropertyGroup.objects.filter(landlord=landlord)
        if group.name.casefold() in lowered
    ]
    if not matched_groups:
        return None
    matched_groups.sort(key=lambda group: len(group.name), reverse=True)
    group = matched_groups[0]
    group_match = re.search(
        rf"\b(?:in|to|under)\s+(?:the\s+)?{re.escape(group.name)}\b",
        text,
        flags=re.IGNORECASE,
    )
    if not group_match:
        return None
    lead = text[: group_match.start()]
    # create_group_room is intentionally singular. Plural/range requests must
    # stay with the collected-preview path so every requested room is retained
    # under one confirmation.
    if re.search(
        r"\brooms\b|\broom\s+\w+\s*(?:,|and|through)\s*(?:room\s+)?\w+",
        lead,
        flags=re.IGNORECASE,
    ):
        return None
    room_name = re.sub(
        r"^\s*(?:please\s+)?(?:create|add|make)\s+"
        r"(?:a\s+)?(?:new\s+)?(?:room\s+)?(?:named\s+|called\s+)?",
        "",
        lead,
        flags=re.IGNORECASE,
    ).strip(" ,.-")
    room_name = re.sub(r"^room\s+", "", room_name, flags=re.IGNORECASE).strip()
    if not room_name or len(room_name) > 255:
        return None

    inventory: list[str] = []
    for pattern, label in (
        (r"\bqueen(?:-size[dn]?)?\s+bed\b", "Queen bed"),
        (r"\bqueen(?:-size[dn]?)?\s+mattress\b", "Queen mattress"),
        (r"\bking(?:-size[dn]?)?\s+bed\b", "King bed"),
        (r"\bking(?:-size[dn]?)?\s+mattress\b", "King mattress"),
        (r"\bdouble\s+bed\b", "Double bed"),
        (r"\bsingle\s+bed\b", "Single bed"),
        (r"\btwin\s+bed\b", "Twin bed"),
        (r"\bmattress\b", "Mattress"),
        (r"\bdesk\b", "Desk"),
        (r"\bdresser\b", "Dresser"),
        (r"\bnightstand\b|\bbedside table\b", "Nightstand"),
    ):
        if (
            label == "Mattress"
            and any(item.casefold().endswith("mattress") for item in inventory)
        ):
            continue
        if re.search(pattern, lowered) and label not in inventory:
            inventory.append(label)

    area_types = parse_common_area_types(text)
    from rentium.properties.models import PropertyArea

    shared_areas = ", ".join(
        str(PropertyArea.AreaType(area_type).label) for area_type in area_types
    )
    classification = ""
    if "landlord" in lowered:
        if re.search(
            r"\b(landlord|owner).{0,30}\b(does not|doesn't|not|never|no)\b"
            r"|\b(no|not).{0,30}\b(landlord|owner)\b",
            lowered,
        ):
            classification = "no"
        elif re.search(
            r"\b(landlord|owner).{0,40}\b(use|uses|share|shares|live|lives)\b"
            r"|\bshared? with (?:the )?(landlord|owner)\b",
            lowered,
        ):
            classification = "yes"
    return {
        "tool": "create_group_room",
        "arguments": {
            "name": room_name,
            "group_name": group.name,
            "inventory_items": ", ".join(inventory),
            "shared_areas": shared_areas,
            "shared_with_landlord": classification,
        },
    }


def _group_room_clarification(landlord, conversation_id) -> dict | None:
    rows = RamaAudit.objects.filter(
        landlord=landlord,
        conversation_id=conversation_id,
        kind=RamaAudit.Kind.TOOL_CALL,
    ).order_by("-created_at")[:20]
    for row in rows:
        content = row.content or {}
        if content.get("tool") != "create_group_room":
            continue
        result = content.get("result") or {}
        if not result.get("needs_input") or result.get("missing_field") != (
            "shared_with_landlord"
        ):
            return None
        latest_assistant = (
            RamaAudit.objects.filter(
                landlord=landlord,
                conversation_id=conversation_id,
                kind=RamaAudit.Kind.ASSISTANT_MESSAGE,
                created_at__gte=row.created_at,
            )
            .order_by("-created_at")
            .first()
        )
        if latest_assistant is None:
            return None
        return {
            "tool": "create_group_room",
            "arguments": dict(content.get("arguments") or {}),
        }
    return None


_PROVINCE_FULL_NAMES = (
    "alberta|british columbia|manitoba|new brunswick|"
    "newfoundland and labrador|newfoundland|nova scotia|"
    "northwest territories|nunavut|ontario|prince edward island|"
    "quebec|saskatchewan|yukon"
)
_PROVINCE_CODES = "AB|BC|MB|NB|NL|NS|NT|NU|ON|PE|QC|SK|YT"
_NUMBER_WORDS = {
    "one": 1,
    "two": 2,
    "three": 3,
    "four": 4,
    "five": 5,
    "six": 6,
    "seven": 7,
    "eight": 8,
}
MAX_HOUSE_CITY_CHARS = 60
MAX_HOUSE_ROOMS = 8
MCC_PREFIX_LENGTH = 3
MIN_HOUSE_ROOMS = 2


def _smart_title(value: str) -> str:
    words: list[str] = []
    for word in re.split(r"(\s+)", str(value or "").strip()):
        if not word or word.isspace():
            words.append(word)
            continue
        titled = word.lower().capitalize()
        if (
            titled.casefold().startswith("mcc")
            and len(titled) > MCC_PREFIX_LENGTH
        ):
            titled = "McC" + titled[3].lower() + titled[4:]
        words.append(titled)
    return "".join(words)


def _canonical_street_address(raw: str) -> str:
    address = _smart_title(raw)
    suffixes = {
        " St": " Street",
        " Ave": " Avenue",
        " Rd": " Road",
        " Dr": " Drive",
        " Blvd": " Boulevard",
        " Cres": " Crescent",
        " Ct": " Court",
    }
    for short, full in suffixes.items():
        if address.endswith(short):
            return address[: -len(short)] + full
    return address


def _location_from_text(text: str) -> tuple[str, str]:
    """Extract an explicitly supplied Canadian city/province pair."""
    from rentium.properties.models import normalise_province

    raw = str(text or "")
    province_match = re.search(
        rf"\b({_PROVINCE_FULL_NAMES})\b",
        raw,
        flags=re.IGNORECASE,
    )
    if province_match is None:
        # Two-letter codes are accepted when capitalized, after a comma, or
        # explicitly labelled as a province. This prevents ordinary prose such
        # as "on the main floor" from being misread as Ontario.
        province_match = re.search(rf"\b({_PROVINCE_CODES})\b", raw)
    if province_match is None:
        province_match = re.search(
            rf"(?:,|\bprovince\s*(?:is|=|:)?)\s*({_PROVINCE_CODES})\b",
            raw,
            flags=re.IGNORECASE,
        )
    if province_match is None:
        return "", ""
    province = normalise_province(province_match.group(1))
    if not province:
        return "", ""

    explicit_city = re.search(
        r"\bcity\s*(?:is|=|:)?\s*([A-Za-z][A-Za-z .'-]{1,50}?)"
        r"(?=\s+(?:and\s+)?(?:the\s+)?province\b|[,.;]|$)",
        raw,
        flags=re.IGNORECASE,
    )
    if explicit_city:
        return _smart_title(explicit_city.group(1).strip()), province

    prefix = raw[: province_match.start()].strip(" ,.;:-")
    prefix = re.sub(
        r"^(?:it(?:'s| is)?\s+)?(?:located\s+)?(?:in\s+)?",
        "",
        prefix,
        flags=re.IGNORECASE,
    )
    # Follow-up answers are commonly "Regina, SK, and no landlord use".
    candidate = re.split(r"[,.;]|\band\b", prefix, flags=re.IGNORECASE)[-1]
    candidate = re.sub(
        r"^(?:the\s+)?(?:city\s+is\s+|city\s*:?\s*)",
        "",
        candidate.strip(),
        flags=re.IGNORECASE,
    ).strip()
    if (
        not candidate
        or len(candidate) > MAX_HOUSE_CITY_CHARS
        or re.search(r"\d", candidate)
    ):
        return "", province
    return _smart_title(candidate), province


def _landlord_sharing_from_text(text: str) -> str:
    lowered = " ".join(str(text or "").casefold().split())
    subject = r"(?:landlord|owner|immediate relative|relative)"
    if re.search(
        rf"\b{subject}\b.{{0,45}}\b(?:does not|doesn't|won't|not|never)\b"
        rf"|\b(?:no|not|never)\b.{{0,45}}\b{subject}\b",
        lowered,
    ):
        return "no"
    if re.search(
        rf"\b{subject}\b.{{0,45}}\b(?:use|uses|share|shares|live|lives)\b"
        rf"|\bshared?\s+with\s+(?:the\s+)?{subject}\b",
        lowered,
    ):
        return "yes"
    trailing = re.search(r"(?:^|[,;]|\band\b)\s*(yes|no)\s*[.!]?\s*$", lowered)
    return trailing.group(1) if trailing else ""


def _house_layout_intent(  # noqa: PLR0911 - each guard rejects ambiguity
    message: str,
) -> dict | None:
    """Parse a high-confidence house/group/room hierarchy instruction.

    This intentionally recognizes only a complete structural pattern. The
    exact legal/location gaps remain blank so the real tool asks one focused
    clarification instead of the model inventing them or returning "resend".
    """
    text = str(message or "")
    lowered = " ".join(text.casefold().split())
    if not (
        re.search(r"\b(?:add|create|make)\b.{0,30}\b(?:another\s+)?house\b", lowered)
        and re.search(r"\bproperty groups?\b", lowered)
        and "basement" in lowered
        and re.search(r"\bmain\s+floor\b", lowered)
    ):
        return None
    address_match = re.search(
        r"\b(\d{1,6}\s+[A-Za-z][A-Za-z0-9 .'-]*?\s+"
        r"(?:street|st|avenue|ave|road|rd|drive|dr|boulevard|blvd|"
        r"crescent|cres|court|ct|lane|way))\b",
        text,
        flags=re.IGNORECASE,
    )
    if address_match is None:
        return None
    address = _canonical_street_address(address_match.group(1))
    street_stem = re.sub(
        r"^\d+\s+|\s+(?:street|avenue|road|drive|boulevard|crescent|court|lane|way)$",
        "",
        address,
        flags=re.IGNORECASE,
    ).strip()
    if not street_stem:
        return None

    count_match = re.search(
        r"\bmain\s+floor\b.{0,100}?\b("
        + "|".join([r"\d+", *_NUMBER_WORDS])
        + r")\s+(?:private\s+)?rooms?\b",
        lowered,
    )
    if count_match is None:
        return None
    raw_count = count_match.group(1)
    room_count = (
        int(raw_count) if raw_count.isdigit() else _NUMBER_WORDS.get(raw_count, 0)
    )
    if room_count < MIN_HOUSE_ROOMS or room_count > MAX_HOUSE_ROOMS:
        return None
    has_private_master_bath = bool(
        re.search(
            r"\b(?:one\s+of\s+the\s+)?(?:washrooms?|bathrooms?)\b"
            r".{0,80}\bprivate\b.{0,80}\bmaster\b"
            r"|\bmaster\b.{0,80}\bprivate\b.{0,80}\b(?:washroom|bathroom)\b",
            lowered,
        ),
    )
    has_subset_shared_bath = bool(
        re.search(
            r"\b(?:other|second)\s+(?:washroom|bathroom)\b.{0,80}\bshared\b"
            r".{0,80}\b(?:other\s+)?two\s+rooms?\b",
            lowered,
        ),
    )
    if not has_private_master_bath or not has_subset_shared_bath:
        return None

    master_name = f"{street_stem} Master Bedroom"
    other_names = [
        f"{street_stem} Main Floor Room {index}"
        for index in range(2, room_count + 1)
    ]
    all_names = [master_name, *other_names]
    rooms = [
        {"name": master_name, "private_areas": ["BATHROOM"]},
        *[{"name": name, "private_areas": []} for name in other_names],
    ]
    shared_areas = [
        {
            "area_type": "BATHROOM",
            "rooms": other_names,
            "description": "Shared washroom for the non-master rooms.",
        },
    ]
    if re.search(r"\bkitchen\b", lowered):
        shared_areas.append({"area_type": "KITCHEN", "rooms": all_names})
    if re.search(r"\bliving\s+room\b", lowered):
        shared_areas.append({"area_type": "LIVING_ROOM", "rooms": all_names})
    city, province = _location_from_text(text)
    layout = {
        "groups": [
            {
                "name": f"{street_stem} Basement",
                "description": "Basement property group; rooms can be added later.",
                "rooms": [],
                "shared_areas": [],
            },
            {
                "name": f"{street_stem} Main Floor",
                "rooms": rooms,
                "shared_areas": shared_areas,
            },
        ],
    }
    return {
        "tool": "create_house_layout",
        "arguments": {
            "holding_name": address,
            "address": address,
            "city": city,
            "province": province,
            "layout_json": json.dumps(layout, separators=(",", ":")),
            "shared_with_landlord": _landlord_sharing_from_text(text),
        },
    }


def _house_layout_clarification(landlord, conversation_id) -> dict | None:
    rows = RamaAudit.objects.filter(
        landlord=landlord,
        conversation_id=conversation_id,
        kind=RamaAudit.Kind.TOOL_CALL,
    ).order_by("-created_at")[:30]
    latest_assistant = (
        RamaAudit.objects.filter(
            landlord=landlord,
            conversation_id=conversation_id,
            kind=RamaAudit.Kind.ASSISTANT_MESSAGE,
        )
        .order_by("-created_at")
        .first()
    )
    for row in rows:
        content = row.content or {}
        if content.get("tool") != "create_house_layout":
            continue
        result = content.get("result") or {}
        question = str(result.get("question_for_user") or "").strip()
        if (
            not result.get("needs_input")
            or latest_assistant is None
            or latest_assistant.created_at < row.created_at
            or str((latest_assistant.content or {}).get("text") or "").strip()
            != question
        ):
            return None
        return {
            "arguments": dict(content.get("arguments") or {}),
            "missing_fields": list(result.get("missing_fields") or []),
        }
    return None


def _merge_house_layout_answer(arguments: dict, message: str) -> dict:
    merged = dict(arguments)
    city, province = _location_from_text(message)
    if city:
        merged["city"] = city
    if province:
        merged["province"] = province
    classification = _landlord_sharing_from_text(message)
    if not classification and (
        _is_affirmative(message) or _is_negative(message)
    ):
        classification = "yes" if _is_affirmative(message) else "no"
    if classification:
        merged["shared_with_landlord"] = classification
    return merged


def _preview_reply(tool: str, result: dict) -> str:
    preview = result.get("preview") or {}
    if tool == "create_house_layout":
        holding = preview.get("holding") or {}
        lines = [
            (
                f"Preview: add house {holding.get('address')} in "
                f"{holding.get('city')}, {str(holding.get('province') or '').upper()} "
                f"({holding.get('action') or 'create'} holding)."
            ),
            "Property groups:",
        ]
        for group in preview.get("groups") or []:
            room_text = (
                f"{group['room_count']} room(s)"
                if group.get("room_count")
                else "empty for now"
            )
            lines.append(
                f"• {group.get('name')} — {room_text}; {group.get('action')}",
            )
        lines.append("Rooms and private areas:")
        for room in preview.get("rooms") or []:
            private = ", ".join(room.get("private_areas") or []) or "none"
            lines.append(
                f"• {room.get('name')} — private areas: {private}; "
                f"{room.get('action')}",
            )
        lines.append("Shared-area access:")
        for area in preview.get("shared_areas") or []:
            classification = (
                "landlord also uses it"
                if area.get("shared_with_landlord")
                else "tenant-only"
            )
            lines.append(
                f"• {area.get('name')} in {area.get('group')} — "
                f"{', '.join(area.get('rooms') or [])}; {classification}",
            )
        conflicts = preview.get("near_duplicate_names") or []
        if conflicts:
            lines.append(
                "Similar existing listing names: "
                + ", ".join(
                    [str(item.get("name") or "") for item in conflicts],
                )
                + ".",
            )
        lines.append(
            "Reply yes to create this complete layout in one transaction, "
            "or no to cancel.",
        )
        return "\n".join(lines)
    if tool == "update_property":
        before = preview.get("property") or "the listing"
        after = (preview.get("changes") or {}).get("name")
        conflicts = preview.get("name_conflicts") or []
        text = f"Preview: rename {before} to {after}."
        if conflicts:
            text += " Similar listing names: " + ", ".join(
                conflict["name"] for conflict in conflicts
            ) + "."
        return text + " Reply yes to confirm, or no to cancel."
    if tool == "create_group_room":
        inventory = preview.get("private_inventory_to_create") or []
        areas = preview.get("shared_areas") or []
        text = (
            f"Preview: create {preview.get('name')} in "
            f"{preview.get('property_group')} at "
            f"{(preview.get('derived_property_data') or {}).get('address')}."
        )
        text += "\nPrivate inventory: " + (
            ", ".join(inventory) if inventory else "none"
        ) + "."
        text += "\nShared areas: " + (
            ", ".join(
                f"{area['name']} "
                f"({'landlord also uses it' if area['shared_with_landlord'] else 'tenant-only'})"
                for area in areas
            )
            if areas
            else "none"
        ) + "."
        conflicts = preview.get("name_conflicts") or []
        if conflicts:
            text += "\nSimilar listing names: " + ", ".join(
                conflict["name"] for conflict in conflicts
            ) + "."
        return text + "\nReply yes to confirm the complete operation, or no to cancel."
    return (
        f"Preview for {tool.replace('_', ' ')}: "
        f"{json.dumps(preview, default=str)}. Reply yes to confirm, or no to cancel."
    )


def _pending_spec_key(spec: dict) -> str:
    """Stable identity for de-duplicating repeated previews in one turn."""
    if spec.get("kind") == "plan":
        return "plan:" + json.dumps(
            spec.get("payload") or {},
            sort_keys=True,
            default=str,
        )
    arguments = {
        key: value
        for key, value in (spec.get("arguments") or {}).items()
        if key != "confirm"
    }
    return (
        f"single:{spec.get('tool')}:"
        + json.dumps(arguments, sort_keys=True, default=str)
    )


def _append_pending_spec(pending_specs: list[dict], spec: dict) -> bool:
    """Append a preview once. Returns True when it was newly collected."""
    key = _pending_spec_key(spec)
    if any(_pending_spec_key(existing) == key for existing in pending_specs):
        return False
    pending_specs.append(spec)
    return True


def _remove_executed_preview(
    pending_specs: list[dict],
    tool: str,
    arguments: dict,
) -> None:
    """Drop only the preview matching a write the model already completed.

    Model-issued confirmation is now stripped before execution, but retaining
    exact removal keeps this invariant safe for non-confirming/idempotent tools
    and avoids the old behaviour of clearing every preview for the same tool.
    """
    wanted = _pending_spec_key(
        {"kind": "single", "tool": tool, "arguments": arguments}
    )
    pending_specs[:] = [
        spec for spec in pending_specs if _pending_spec_key(spec) != wanted
    ]


def _batch_preview_reply(
    plan: RamaPendingPlan,
    excluded_errors: list[dict] | None = None,
) -> str:
    """Truthful deterministic preview for a collected multi-write turn."""
    lines = [
        f"Preview — one “Yes” will run all {plan.steps.count()} changes:"
    ]
    for index, step in enumerate(plan.steps.order_by("order"), start=1):
        args = step.arguments or {}
        target = step.target_label or str(
            args.get("property_query") or args.get("name") or "item"
        )
        if step.tool == "create_property":
            detail = f"Create {args.get('name') or target}"
            if args.get("group_name"):
                detail += f" in {args['group_name']}"
            if args.get("address"):
                detail += f" at {args['address']}"
            locality = ", ".join(
                str(value)
                for value in (args.get("city"), args.get("province"))
                if value
            )
            if locality:
                detail += f", {locality}"
            if args.get("inventory_items"):
                detail += f"; private inventory: {args['inventory_items']}"
        elif step.tool == "create_group_room":
            detail = (
                f"Create {args.get('name') or target} in "
                f"{args.get('group_name') or 'property group'}"
            )
            if args.get("address"):
                detail += f" at {args['address']}"
            if args.get("inventory_items"):
                detail += f"; private inventory: {args['inventory_items']}"
            if args.get("shared_areas"):
                detail += f"; shared areas: {args['shared_areas']}"
                if args.get("shared_with_landlord"):
                    detail += (
                        "; landlord/immediate-relative use: "
                        f"{args['shared_with_landlord']}"
                    )
        elif step.tool == "update_property" and args.get("name"):
            detail = f"Rename {target} to {args['name']}"
        elif step.tool == "assign_property_to_group":
            if str(args.get("clear") or "").casefold() in {"yes", "true", "1"}:
                detail = f"Remove {target} from its property group"
            else:
                detail = f"Put {target} in {args.get('group_name')}"
        elif step.tool == "create_property_group":
            detail = f"Create property group {args.get('name') or target}"
        else:
            detail = (
                f"{step.tool.replace('_', ' ').capitalize()}: {target}"
            )
        lines.append(f"{index}. {detail}")

    if excluded_errors:
        lines.append("")
        lines.append("Not included:")
        for item in excluded_errors:
            lines.append(f"• {item['target']}: {item['error']}")
    lines.append("")
    lines.append("Reply yes to confirm the complete batch, or no to cancel.")
    return "\n".join(lines)


def _replacement_request(message: str) -> bool:
    """A rejection that also supplies corrected work, not a bare cancellation."""
    return bool(
        re.match(
            r"^\s*(?:no|nope|nah|cancel(?:\s+that)?|don't|do not)\b"
            r"[\s,:;.-]+"
            r"(?:instead\b|make\b|change\b|replace\b|use\b|do\s+this\b|"
            r"like\s+this\b|it\s+should\b|the\s+correct\b).+",
            message or "",
            flags=re.IGNORECASE | re.DOTALL,
        )
    )


def _creation_defaults_from_plan(plan: RamaPendingPlan | None) -> list[dict]:
    if plan is None:
        return []
    return [
        dict(step.arguments)
        for step in plan.steps.order_by("order")
        if step.tool == "create_property"
    ]


def _recent_creation_defaults(landlord, conversation_id) -> list[dict]:
    """Recover location defaults from earlier *executable* creation plans.

    This is deliberately structural: it reads audited tool arguments and nested
    delegated plans, never assistant prose. It lets a correction such as
    "No, make it like this: [new room names]" retain the already-previewed
    address/city/province without asking the model to reconstruct them.
    """
    rows = RamaAudit.objects.filter(
        landlord=landlord,
        conversation_id=conversation_id,
        kind=RamaAudit.Kind.TOOL_CALL,
    ).order_by("-created_at")[:80]
    live_defaults: list[dict] = []
    planned_defaults: list[dict] = []
    for row in rows:
        content = row.content or {}
        if content.get("tool") == "_live_context":
            result = content.get("result") or {}
            for listing in (
                list(result.get("rooms") or [])
                + list(result.get("complete_units") or [])
                + list(result.get("listings") or [])
            ):
                if listing.get("address") and listing.get("city"):
                    live_defaults.append(
                        {
                            "name": listing.get("name"),
                            "address": listing.get("address"),
                            "city": listing.get("city"),
                            "province": listing.get("province"),
                            "group_name": listing.get("group"),
                        }
                    )
        if content.get("tool") == "create_property":
            planned_defaults.append(dict(content.get("arguments") or {}))
        plan = (content.get("result") or {}).get("plan") or {}
        for step in plan.get("steps") or []:
            if step.get("tool") == "create_property":
                planned_defaults.append(dict(step.get("arguments") or {}))
    # Recorded portfolio facts outrank model-prepared arguments. Historical
    # snapshots remain useful after the landlord deletes rooms specifically to
    # rebuild their layout at the same physical holding.
    return live_defaults + planned_defaults


def _enrich_empty_group_room_arguments(
    landlord,
    conversation_id,
    arguments: dict,
) -> dict:
    """Carry audited location facts into create_group_room retries.

    Weak models often retry a corrected batch from a bare "Yes" and omit the
    address that appeared two messages earlier. We recover only arguments from
    prior executable create_property plans, then resolve the holding by exact
    recorded address. No assistant prose or fuzzy address guess is trusted.
    """
    from rentium.properties.models import PropertyHolding

    enriched = dict(arguments)
    if any(
        str(enriched.get(key) or "").strip()
        for key in ("holding_name", "address", "city", "province")
    ):
        return enriched
    group_key = " ".join(
        str(enriched.get("group_name") or "").casefold().split()
    )
    if not group_key:
        return enriched
    for candidate in _recent_creation_defaults(landlord, conversation_id):
        candidate_group = " ".join(
            str(candidate.get("group_name") or "").casefold().split()
        )
        if candidate_group != group_key:
            continue
        address = str(candidate.get("address") or "").strip()
        if not address:
            continue
        holdings = list(
            PropertyHolding.objects.filter(
                landlord=landlord,
                address__iexact=address,
            )[:2]
        )
        if len(holdings) != 1:
            continue
        holding = holdings[0]
        enriched["holding_name"] = holding.name
        enriched["address"] = holding.address
        if not holding.city:
            enriched["city"] = str(candidate.get("city") or "")
        enriched["province"] = str(candidate.get("province") or "")
        return enriched
    return enriched


def _explicit_room_creation_rows(
    landlord,
    conversation_id,
    message: str,
    pending_plan: RamaPendingPlan | None = None,
) -> list[dict] | None:
    """Parse an explicit numbered "Create X in GROUP at ADDRESS" batch.

    This is intentionally narrow and high-confidence. It handles the exact
    correction format landlords naturally use while leaving free-form planning
    to the model. Every parsed row is still previewed through create_property,
    so validation, duplicate checks, tenancy scoping, and confirmation remain
    unchanged.
    """
    from rentium.properties.models import PropertyGroup

    groups = list(
        PropertyGroup.objects.filter(landlord=landlord).order_by("-name")
    )
    if not groups:
        return None
    candidates = re.findall(
        r"(?im)^\s*(?:\d+\s*[.)-]\s*)?create\s+.+$",
        message or "",
    )
    if len(candidates) < 2:
        return None

    parsed: list[tuple[str, object, str]] = []
    for raw_line in candidates:
        line = re.sub(
            r"^\s*(?:\d+\s*[.)-]\s*)?create\s+",
            "",
            raw_line,
            flags=re.IGNORECASE,
        ).strip(" \t\r\n\"'")
        matched = None
        for group in sorted(groups, key=lambda item: len(item.name), reverse=True):
            match = re.match(
                rf"^(.+?)\s+in\s+{re.escape(group.name)}\s+at\s+(.+?)$",
                line,
                flags=re.IGNORECASE,
            )
            if match:
                matched = (
                    match.group(1).strip(" ,.-"),
                    group,
                    match.group(2).strip(" ,.-"),
                )
                break
        if matched is None:
            return None
        parsed.append(matched)

    defaults = _recent_creation_defaults(landlord, conversation_id)
    defaults.extend(_creation_defaults_from_plan(pending_plan))

    def location_for(group_name: str, address: str) -> dict | None:
        group_key = " ".join(group_name.casefold().split())
        address_key = " ".join(address.casefold().split())
        for candidate in defaults:
            candidate_group = " ".join(
                str(candidate.get("group_name") or "").casefold().split()
            )
            candidate_address = " ".join(
                str(candidate.get("address") or "").casefold().split()
            )
            if candidate_group == group_key and candidate_address == address_key:
                if candidate.get("city") and candidate.get("province"):
                    return candidate
        for candidate in defaults:
            candidate_address = " ".join(
                str(candidate.get("address") or "").casefold().split()
            )
            if candidate_address == address_key:
                if candidate.get("city") and candidate.get("province"):
                    return candidate
        return None

    rows: list[dict] = []
    for name, group, address in parsed:
        location = location_for(group.name, address)
        if location is None:
            # An address alone is not enough to safely invent city/province.
            # Fall back to the model, which can ask the landlord explicitly.
            return None
        rows.append(
            {
                "name": name,
                "address": address,
                "city": str(location["city"]),
                "province": str(location["province"]),
                "property_category": "ROOM",
                "room_type": "PRIVATE",
                "group_name": group.name,
            }
        )
    return rows


def _looks_like_confirmation_request(text: str) -> bool:
    """True for model prose claiming that an executable preview exists."""
    return bool(
        re.search(
            r"\breply\s+(?:yes|y)\s+to\s+confirm\b"
            r"|\bconfirm\s+yes\b"
            r"|\bone\s+[“\"']?yes[”\"']?\s+will\s+run\b",
            text or "",
            flags=re.IGNORECASE,
        )
    )


def _persist_pending(
    landlord,
    conversation_id,
    pending_specs: list[dict] | None,
) -> None:
    """End-of-turn invariant: a plan row exists IFF something is outstanding.

    - Every preview produced this turn is persisted in call order as one plan
      (replacing any older one).
    - No preview: keep the prior plan. A side question must not silently destroy
      confirmed intent; only an explicit no/cancel, a corrected replacement, or
      a newly previewed plan replaces it.
    """
    if not pending_specs:
        return
    save_batch(landlord, conversation_id, pending_specs)


def _tool_facts_note(
    landlord, conversation_id, limit: int = 8, budget_chars: int = 2000
) -> list[str]:
    """Compact fact lines from this conversation's earlier tool calls.

    Text-only history made tool-discovered facts evaporate between turns —
    the structural cause of the hallucination class. These digests re-ground
    them; LIVE PORTFOLIO still wins on any conflict.
    """
    from .digests import digest_tool_call

    rows = RamaAudit.objects.filter(
        landlord=landlord,
        conversation_id=conversation_id,
        kind=RamaAudit.Kind.TOOL_CALL,
    ).order_by("-created_at")[: limit * 3]
    lines: list[str] = []
    used = 0
    for r in reversed(list(rows)):
        content = r.content or {}
        tool = content.get("tool", "")
        if not tool or tool.startswith("_"):
            continue  # _live_context / _plan_cancelled are not facts
        line = digest_tool_call(tool, content.get("arguments"), content.get("result"))
        if not line or line in lines:
            continue
        if used + len(line) > budget_chars or len(lines) >= limit:
            break
        lines.append(line)
        used += len(line)
    return lines


def _recent_writes_note(landlord, conversation_id, limit: int = 10) -> list[str]:
    """Short list of writes already performed earlier in this conversation, so
    the model has continuity across turns and never re-creates what it made."""
    rows = (
        RamaAudit.objects.filter(
            landlord=landlord,
            conversation_id=conversation_id,
            kind=RamaAudit.Kind.TOOL_CALL,
        )
        .order_by("-created_at")[:limit]
    )
    notes: list[str] = []
    for r in reversed(list(rows)):
        content = r.content or {}
        tool = content.get("tool", "")
        res = content.get("result")
        if not tool or tool == "_live_context" or not isinstance(res, dict):
            continue
        if res.get("created"):
            notes.append(f"{tool}: created {_write_label(res)}".strip())
        elif res.get("deleted"):
            notes.append(f"{tool}: deleted {res.get('property') or ''}".strip())
        elif res.get("updated"):
            notes.append(f"{tool}: updated {_write_label(res)}".strip())
        elif res.get("workflow") and res.get("done"):
            ident = res.get("lease_number") or res.get("property_name") or ""
            notes.append(f"{tool}: completed {ident}".strip())
    seen: set[str] = set()
    deduped: list[str] = []
    for n in notes:
        if n not in seen:
            seen.add(n)
            deduped.append(n)
    return deduped[-6:]


def _delegate(landlord, tool_name: str, arguments: dict) -> dict:
    """Run a bounded sub-turn for a delegation call from the General.

    The sub-agent's pending plan (if any) is re-homed onto the caller by
    returning it as a needs_confirm payload — the outer turn persists it on
    the GENERAL's conversation, so the landlord's next "yes" runs it through
    the same deterministic confirm machine.
    """
    sub_role = "fsa" if tool_name == "ask_fsa" else "corporal"
    instruction = str(
        (arguments or {}).get("instruction")
        or (arguments or {}).get("question")
        or ""
    ).strip()
    if not instruction:
        return {"error": "instruction is required."}

    sub_cid = uuid.uuid4()
    sub = run_turn(
        landlord, instruction, sub_cid, role=sub_role, channel="system", depth=1
    )
    if sub.error is not None:
        return {"error": f"{sub_role} unavailable: {sub.error['detail']}"}

    out: dict = {"role": sub_role, "answer": sub.reply, "tools_used": sub.tools_used}
    plan_row = load_fresh_plan(landlord, sub_cid)
    if plan_row is not None:
        payload = plan_to_payload(plan_row)
        clear_plan(landlord, sub_cid)
        out["needs_confirm"] = True
        out["plan"] = payload
        out["instruction"] = (
            f"The {sub_role} prepared a plan. Show the landlord ALL its steps "
            "and blocked items, then STOP — the system handles their yes/no."
        )
    return out


@dataclass
class TurnResult:
    conversation_id: uuid.UUID
    reply: str = ""
    provider: str = ""
    model: str = ""
    tools_used: list[str] = field(default_factory=list)
    pending_plan: dict | None = None
    deterministic: bool = False
    # Files a delivery tool asked the CHANNEL to send (e.g. a lease PDF, property
    # photos). The web ignores these; Telegram/WhatsApp turn them into real
    # attachments. Each is the tool's `_attachment` marker.
    attachments: list[dict] = field(default_factory=list)
    # Set instead of raising: {"detail": str, "code": str, "status_hint": int}
    error: dict | None = None


def run_turn(
    landlord,
    message: str,
    conversation_id: uuid.UUID | None = None,
    *,
    role: str = "corporal",
    channel: str = "web",
    depth: int = 0,
    extra_system: str = "",
) -> TurnResult:
    """Run one agent turn for `landlord`. Callers validate enablement/input;
    this function assumes a non-empty message and a resolvable provider."""
    conversation_id = conversation_id or uuid.uuid4()
    cfg = get_role_config(landlord, role)
    provider_name, model, api_key = cfg.provider, cfg.model, cfg.api_key

    meta = {}
    if role != "corporal":
        meta["role"] = role
    if channel != "web":
        meta["channel"] = channel

    def audit(kind, content):
        RamaAudit.objects.create(
            landlord=landlord,
            conversation_id=conversation_id,
            kind=kind,
            provider=provider_name,
            model=model,
            content={**content, **meta} if meta else content,
        )

    try:
        provider = get_provider(provider_name)
    except ProviderError as exc:
        return TurnResult(
            conversation_id=conversation_id,
            provider=provider_name,
            model=model,
            error={"detail": str(exc), "code": "PROVIDER_ERROR", "status_hint": 400},
        )

    prior = RamaAudit.objects.filter(
        landlord=landlord,
        conversation_id=conversation_id,
        kind__in=[RamaAudit.Kind.USER_MESSAGE, RamaAudit.Kind.ASSISTANT_MESSAGE],
    ).order_by("created_at")
    messages = []
    for row in list(prior)[-HISTORY_TURNS:]:
        text = row.content.get("text", "")
        if not text:
            continue
        if row.kind == RamaAudit.Kind.USER_MESSAGE:
            messages.append({"role": "user", "content": text})
        else:
            messages.append({"role": "assistant", "text": text})
    messages.append({"role": "user", "content": message})

    audit(RamaAudit.Kind.USER_MESSAGE, {"text": message})

    # Fresh DB snapshot every turn so the model cannot invent or stick to
    # wrong numbers from earlier assistant turns (history has no tool results).
    try:
        context = live_context(landlord)
        safe_context = json.loads(json.dumps(context, default=str))
    except Exception as exc:  # noqa: BLE001
        safe_context = {"error": f"live_context failed: {exc}"}
    system = ROLE_PROMPTS[role]
    ctx = role_context(role, landlord)
    if ctx:
        system += "\n\n" + ctx
    # Phase 4: the generic read/update/link surface is described by DATA (the
    # manifest), not persona prose — so it grows automatically as the manifest
    # does, and the prompt never needs a new hand-written line per capability.
    if role in ("corporal", "general"):
        from .manifest import capability_digest

        system += "\n\n" + capability_digest()
    system += (
        "\n\n## LIVE PORTFOLIO (authoritative — overrides chat history)\n"
        + json.dumps(safe_context, indent=None, separators=(",", ":"))
    )
    if channel in ("telegram", "whatsapp"):
        system += (
            "\n\n## MESSAGING STYLE (you are in a " + channel + " chat)\n"
            "Write like a person texting: warm, natural, concise, professional. "
            "PLAIN TEXT ONLY — no markdown: never use *, **, _, #, backticks, "
            "tables, or [label](url) links. Write any URL bare. Use short "
            "sentences; if you list things, use '• ' bullets, at most a handful. "
            "Keep replies short — a few lines, not a long report. To hand over a "
            "lease PDF or a listing's photos, call deliver_lease_pdf / "
            "deliver_property_photos (they send the real file) — never paste an "
            "/api/... URL or a markdown download link."
        )
    if extra_system:
        system += "\n\n" + extra_system
    done_notes = _recent_writes_note(landlord, conversation_id)
    if done_notes:
        system += (
            "\n\n## ALREADY DONE THIS CONVERSATION (do not repeat or re-create)\n"
            + "; ".join(done_notes)
        )
    fact_lines = _tool_facts_note(landlord, conversation_id)
    if fact_lines:
        system += (
            "\n\n## FACTS FROM EARLIER TOOL CALLS THIS CONVERSATION "
            "(subordinate to LIVE PORTFOLIO — if they disagree, LIVE "
            "PORTFOLIO is right)\n- " + "\n- ".join(fact_lines)
        )
    audit(
        RamaAudit.Kind.TOOL_CALL,
        {
            "tool": "_live_context",
            "arguments": {},
            "result": safe_context,
        },
    )

    schemas = role_tool_schemas(role, depth)
    max_rounds = SUB_TURN_MAX_ROUNDS if depth >= 1 else MAX_TOOL_ROUNDS
    tools_used: list[str] = ["_live_context"]
    turn_attachments: list[dict] = []
    turn = Turn()

    def _run_deterministic_tool(tool_name: str, arguments: dict) -> dict:
        result = execute(tool_name, arguments, landlord=landlord)
        safe_result = json.loads(json.dumps(result, default=str))
        tools_used.append(tool_name)
        audit(
            RamaAudit.Kind.TOOL_CALL,
            {
                "tool": tool_name,
                "arguments": arguments,
                "result": safe_result,
                "deterministic_routing": True,
            },
        )
        return result

    def _prepare_explicit_creation_batch(rows: list[dict]) -> str:
        specs: list[dict] = []
        excluded: list[dict] = []
        questions: list[str] = []
        for arguments in rows:
            result = _run_deterministic_tool("create_property", arguments)
            if result.get("needs_confirm"):
                stable_arguments = dict(arguments)
                preview = result.get("preview") or {}
                for field in ("address", "city", "province"):
                    if preview.get(field):
                        stable_arguments[field] = preview[field]
                specs.append(
                    {
                        "kind": "single",
                        "tool": "create_property",
                        "arguments": stable_arguments,
                        "target": arguments["name"],
                    }
                )
            elif result.get("needs_input"):
                questions.append(str(result.get("question_for_user") or ""))
            elif result.get("unchanged") or result.get("idempotent"):
                excluded.append(
                    {
                        "target": arguments["name"],
                        "error": str(
                            result.get("message") or "No change is needed."
                        ),
                    }
                )
            else:
                excluded.append(
                    {
                        "target": arguments["name"],
                        "error": str(result.get("error") or result),
                    }
                )
        if specs:
            plan = save_batch(landlord, conversation_id, specs)
            return _batch_preview_reply(plan, excluded)
        clear_plan(landlord, conversation_id)
        if questions:
            return "\n".join(
                dict.fromkeys(question for question in questions if question)
            )
        if excluded:
            return "Nothing is waiting for confirmation.\n" + "\n".join(
                f"• {item['target']}: {item['error']}" for item in excluded
            )
        return "Nothing is waiting for confirmation."

    # Deterministic confirm state machine: on yes/no the backend itself runs
    # or cancels the previewed PLAN (single writes are one-step plans) — the
    # model never reconstructs tool calls (that was the endless re-preview
    # loop). Lease terminations and similar own-confirm steps pause execution
    # for their own explicit "yes" (tiered confirm).
    plan_progress: dict | None = None
    deterministic_reply: str | None = None
    pending_plan = load_fresh_plan(landlord, conversation_id)
    replacement_rows = (
        _explicit_room_creation_rows(
            landlord,
            conversation_id,
            message,
            pending_plan,
        )
        if pending_plan is not None and _replacement_request(message)
        else None
    )
    if pending_plan is not None and replacement_rows is not None:
        # save_batch replaces the rejected plan only after every replacement
        # row has gone through the real create_property preview contract.
        deterministic_reply = _prepare_explicit_creation_batch(replacement_rows)
    elif pending_plan is not None and _replacement_request(message):
        # The old proposal was explicitly rejected. Keep it only as structured
        # defaults for the model rebuilding the correction; it is no longer
        # something a later bare Yes may execute.
        rejected = plan_to_payload(pending_plan)
        clear_plan(landlord, conversation_id)
        pending_plan = None
        system += (
            "\n\n## REJECTED PLAN (do not execute; use only unchanged facts such "
            "as addresses while preparing the landlord's corrected request)\n"
            + json.dumps(rejected, default=str)
        )
    elif pending_plan is not None and _is_affirmative(message):

        def _plan_audit(content):
            tools_used.append(content.get("tool", ""))
            audit(RamaAudit.Kind.TOOL_CALL, content)

        plan_progress = run_plan(pending_plan, landlord, audit=_plan_audit)
        if _norm_affirm(message) in _AFFIRM_EXACT:
            deterministic_reply = _plan_fallback_reply(plan_progress)
        else:
            # "yes and …" — the model handles the extra instruction, with the
            # already-executed progress pinned as ground truth.
            system += (
                "\n\n## PLAN PROGRESS (landlord confirmed — the system ALREADY "
                "ran this; report exactly what it says, item by item: done/"
                "failed/skipped/awaiting. Do NOT preview or run any of it "
                "again, and do NOT propose further actions beyond the "
                "landlord's message)\n"
                + json.dumps(plan_progress, default=str)
            )
    elif pending_plan is not None and _is_negative(message):
        summary = pending_plan.summary
        clear_plan(landlord, conversation_id)
        audit(
            RamaAudit.Kind.TOOL_CALL,
            {"tool": "_plan_cancelled", "arguments": {}, "result": {"cancelled": summary}},
        )
        deterministic_reply = (
            f"Cancelled — nothing was run. ({summary})"
            if summary
            else "Cancelled — nothing was run."
        )
    elif pending_plan is not None:
        # Interjected question while a plan waits: keep the model aware so it
        # can answer AND remind the landlord the plan is still pending.
        system += (
            "\n\n## PENDING PLAN (awaiting the landlord's confirmation — do "
            "NOT execute or re-preview it; answer their message and mention "
            "the plan is still waiting for a yes/no)\n"
            + json.dumps(plan_brief(pending_plan), default=str)
        )

    # A yes/no immediately after create_group_room asked its legal
    # landlord-sharing question is an answer to that question, not a fresh
    # command and not an action confirmation yet.
    if (
        deterministic_reply is None
        and pending_plan is None
        and (_is_affirmative(message) or _is_negative(message))
    ):
        clarification = _group_room_clarification(landlord, conversation_id)
        if clarification is not None:
            arguments = clarification["arguments"]
            arguments["shared_with_landlord"] = (
                "yes" if _is_affirmative(message) else "no"
            )
            result = _run_deterministic_tool("create_group_room", arguments)
            if result.get("needs_confirm"):
                save_single(
                    landlord,
                    conversation_id,
                    "create_group_room",
                    arguments,
                )
                deterministic_reply = _preview_reply("create_group_room", result)
            else:
                deterministic_reply = str(
                    result.get("error")
                    or result.get("question_for_user")
                    or result.get("message")
                    or result
                )

    # A bare repeated confirmation immediately after an audited plan execution
    # must never become a brand-new model turn. A standalone "yes" with no such
    # execution remains ordinary conversation for backwards compatibility.
    if (
        deterministic_reply is None
        and pending_plan is None
        and _norm_affirm(message) in _AFFIRM_EXACT
    ):
        recent_reply = _recent_confirmed_reply(landlord, conversation_id)
        if recent_reply:
            deterministic_reply = (
                "That confirmation was already applied. No action was repeated.\n"
                + recent_reply
            )

    # A hierarchical house request is one domain operation, not loose prose
    # and not a fragile series of model-authored writes. The deterministic
    # parser captures the understood layout; the composite tool asks only for
    # missing legal/location facts and then persists one atomic preview.
    if deterministic_reply is None and pending_plan is None:
        house_intent = _house_layout_intent(message)
        house_clarification = (
            None
            if house_intent is not None
            else _house_layout_clarification(landlord, conversation_id)
        )
        if house_intent is not None:
            house_arguments = house_intent["arguments"]
        elif house_clarification is not None and _norm_affirm(message) in {
            "cancel",
            "cancel that",
            "cancel it",
            "abort",
            "never mind",
            "nevermind",
            "forget it",
        }:
            house_arguments = None
            deterministic_reply = (
                "Cancelled the house-layout draft. Nothing was created."
            )
        elif house_clarification is not None:
            house_arguments = _merge_house_layout_answer(
                house_clarification["arguments"],
                message,
            )
        else:
            house_arguments = None

        if house_arguments is not None:
            result = _run_deterministic_tool(
                "create_house_layout",
                house_arguments,
            )
            if result.get("needs_input"):
                deterministic_reply = str(result.get("question_for_user"))
            elif result.get("needs_confirm"):
                save_single(
                    landlord,
                    conversation_id,
                    "create_house_layout",
                    house_arguments,
                )
                deterministic_reply = _preview_reply(
                    "create_house_layout",
                    result,
                )
            else:
                deterministic_reply = str(
                    result.get("error")
                    or result.get("message")
                    or result
                )

    # Explicit numbered creation batches are executable syntax, not prose for
    # the model to paraphrase. This also handles a corrected replacement after
    # the old plan was rejected: audited create_property plans provide the
    # unchanged location defaults.
    if deterministic_reply is None and pending_plan is None:
        creation_rows = _explicit_room_creation_rows(
            landlord,
            conversation_id,
            message,
        )
        if creation_rows is not None:
            deterministic_reply = _prepare_explicit_creation_batch(creation_rows)

    # High-confidence property operations bypass model intent selection. A
    # rename can therefore never drift into an availability/status plan.
    if deterministic_reply is None and pending_plan is None:
        intent = _rename_intent(landlord, message)
        if intent is not None:
            result = _run_deterministic_tool(
                intent["tool"], intent["arguments"]
            )
            if result.get("needs_confirm"):
                save_single(
                    landlord,
                    conversation_id,
                    intent["tool"],
                    intent["arguments"],
                )
                deterministic_reply = _preview_reply(intent["tool"], result)
            else:
                deterministic_reply = str(
                    result.get("error") or result.get("message") or result
                )

    if deterministic_reply is None and pending_plan is None:
        collection = _dashboard_collection_intent(message)
        if collection is not None:
            result = _run_deterministic_tool(
                "link", {"entity": collection, "query": ""}
            )
            deterministic_reply = str(
                result.get("error")
                or f"{result.get('label')}: {result.get('link')}"
            )

    if (
        deterministic_reply is None
        and pending_plan is None
        and _show_all_rooms_intent(message)
    ):
        result = _run_deterministic_tool("list_properties", {})
        deterministic_reply = (
            str(result["error"]) if result.get("error") else _rooms_reply(result)
        )

    if deterministic_reply is None and pending_plan is None:
        intent = _group_room_intent(landlord, message)
        if intent is not None:
            result = _run_deterministic_tool(
                intent["tool"], intent["arguments"]
            )
            if result.get("needs_input"):
                deterministic_reply = str(result.get("question_for_user"))
            elif result.get("needs_confirm"):
                save_single(
                    landlord,
                    conversation_id,
                    intent["tool"],
                    intent["arguments"],
                )
                deterministic_reply = _preview_reply(intent["tool"], result)
            else:
                deterministic_reply = str(
                    result.get("error") or result.get("message") or result
                )

    # Bare yes/no is fully handled above — answer without any provider
    # round-trip, so a weak model can never "narrate" actions that didn't
    # happen or spin up a fresh plan after a cancellation.
    if deterministic_reply is not None:
        # A deterministic router may itself have just created a preview plan.
        # Preserve it; confirmations/cancellations have no outstanding plan.
        if load_fresh_plan(landlord, conversation_id) is None:
            _persist_pending(landlord, conversation_id, None)
        outstanding = load_fresh_plan(landlord, conversation_id)
        audit(
            RamaAudit.Kind.ASSISTANT_MESSAGE,
            {
                "text": deterministic_reply,
                "tools_used": tools_used,
                "deterministic": True,
                **(
                    {"confirmation_result": True}
                    if plan_progress is not None
                    else {}
                ),
            },
        )
        return TurnResult(
            conversation_id=conversation_id,
            reply=deterministic_reply,
            provider=provider_name,
            model=model,
            tools_used=tools_used,
            pending_plan=plan_brief(outstanding) if outstanding else None,
            deterministic=True,
        )

    # Every still-outstanding preview produced THIS turn.  They are persisted
    # together, in call order, so the next "yes" runs the complete batch the
    # landlord was shown—not whichever tool happened to be called last.
    pending_specs: list[dict] = []
    excluded_preview_errors: list[dict] = []
    unresolved_write_inputs: list[str] = []
    planned_property_aliases: dict[str, str] = {}
    turn_tools = schemas
    try:
        for _ in range(max_rounds):
            turn = provider.complete(
                model=model,
                system=system,
                messages=messages,
                tools=turn_tools,
                api_key=api_key,
            )
            if not turn.tool_calls:
                break
            messages.append(
                {
                    "role": "assistant",
                    "text": turn.text,
                    "tool_calls": [
                        {
                            "id": c.id,
                            "name": c.name,
                            "arguments": c.arguments,
                            # Pass-through for Gemini thought_signature, etc.
                            "extra": c.extra or {},
                        }
                        for c in turn.tool_calls
                    ],
                }
            )
            for call in turn.tool_calls:
                effective_arguments = dict(call.arguments or {})
                if call.name == "create_group_room":
                    effective_arguments = _enrich_empty_group_room_arguments(
                        landlord,
                        conversation_id,
                        effective_arguments,
                    )
                original_property_query = str(
                    effective_arguments.get("property_query") or ""
                ).strip()
                alias_id = planned_property_aliases.get(
                    " ".join(original_property_query.casefold().split())
                )
                if alias_id:
                    # A later step may refer to the name an earlier preview
                    # will create. Resolve it to the earlier listing's stable
                    # id now, while still preserving the human-facing target.
                    effective_arguments["property_query"] = alias_id

                registered_tool = REGISTRY.get(call.name)
                if (
                    registered_tool is not None
                    and "confirm" in registered_tool.parameters["properties"]
                ):
                    # Only run_plan() may inject confirm=yes after loading a
                    # persisted landlord-approved preview. A model can prepare
                    # writes, but it cannot approve its own proposal.
                    effective_arguments["confirm"] = ""
                if (
                    role == "general"
                    and depth == 0
                    and call.name in DELEGATION_TOOL_NAMES
                ):
                    result = _delegate(landlord, call.name, effective_arguments)
                else:
                    result = execute(call.name, effective_arguments, landlord=landlord)
                # JSON-safe for audit + tool message content (UUIDs, Decimals).
                safe_result = json.loads(json.dumps(result, default=str))
                if isinstance(result, dict) and isinstance(
                    result.get("_attachment"), dict
                ):
                    turn_attachments.append(result["_attachment"])
                tools_used.append(call.name)
                audit(
                    RamaAudit.Kind.TOOL_CALL,
                    {
                        "tool": call.name,
                        "arguments": effective_arguments,
                        "result": safe_result,
                    },
                )
                if isinstance(result, dict) and result.get("needs_confirm"):
                    if isinstance(result.get("plan"), dict):
                        # A playbook plan (plan_operation / plan_move_tenant).
                        spec = {"kind": "plan", "payload": result["plan"]}
                    else:
                        stable_arguments = dict(effective_arguments)
                        preview = result.get("preview") or {}
                        if call.name == "create_property":
                            for field in ("address", "city", "province"):
                                if preview.get(field):
                                    stable_arguments[field] = preview[field]
                        if (
                            call.name == "update_property"
                            and preview.get("id")
                            and str(
                                effective_arguments.get("name") or ""
                            ).strip()
                        ):
                            stable_arguments["property_query"] = str(
                                preview["id"]
                            )
                        target = str(
                            (
                                original_property_query
                                if alias_id and original_property_query
                                else preview.get("property")
                            )
                            or stable_arguments.get("property_query")
                            or stable_arguments.get("room_name")
                            or stable_arguments.get("name")
                            or ""
                        )
                        spec = {
                            "kind": "single",
                            "tool": call.name,
                            "arguments": stable_arguments,
                            "target": target,
                        }
                    _append_pending_spec(pending_specs, spec)

                    # Make the future name addressable by later calls in this
                    # same model turn. The pending step itself already uses the
                    # stable id, so execution remains correct after the rename.
                    if call.name == "update_property":
                        preview = result.get("preview") or {}
                        future_name = str(
                            effective_arguments.get("name") or ""
                        ).strip()
                        if preview.get("id") and future_name:
                            planned_property_aliases[
                                " ".join(future_name.casefold().split())
                            ] = str(preview["id"])
                elif isinstance(result, dict) and (
                    result.get("created")
                    or result.get("updated")
                    or result.get("deleted")
                    or result.get("done")
                ):
                    # A write went through — clear any matching outstanding
                    # preview so we don't ask the landlord to confirm it again.
                    _remove_executed_preview(
                        pending_specs,
                        call.name,
                        effective_arguments,
                    )
                elif (
                    isinstance(result, dict)
                    and result.get("error")
                    and registered_tool is not None
                    and "confirm" in registered_tool.parameters["properties"]
                ):
                    target = (
                        original_property_query
                        or str(effective_arguments.get("name") or "")
                        or call.name.replace("_", " ")
                    )
                    excluded_preview_errors.append(
                        {"target": target, "error": str(result["error"])}
                    )
                elif (
                    isinstance(result, dict)
                    and result.get("needs_input")
                    and registered_tool is not None
                    and "confirm" in registered_tool.parameters["properties"]
                ):
                    question = str(result.get("question_for_user") or "").strip()
                    if question and question not in unresolved_write_inputs:
                        unresolved_write_inputs.append(question)
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": call.id,
                        "name": call.name,
                        "content": json.dumps(safe_result),
                    }
                )
        else:
            partial = (turn.text or "").strip()
            turn = Turn(
                text=(
                    (
                        partial
                        + "\n\n(I hit my step limit for one turn — say 'continue' "
                        "or narrow the request and I'll pick up where I left off.)"
                    )
                    if partial
                    else (
                        "That took more steps than I can do in one turn — ask me to "
                        "continue, or break it into smaller steps."
                    )
                )
            )
    except ProviderError as exc:
        audit(RamaAudit.Kind.ERROR, {"error": str(exc)})
        # If a confirmed plan already ran, don't surface an error for work
        # that actually succeeded — report it deterministically instead.
        if plan_progress is not None:
            _persist_pending(landlord, conversation_id, pending_specs)
            outstanding = load_fresh_plan(landlord, conversation_id)
            reply = _plan_fallback_reply(plan_progress)
            audit(
                RamaAudit.Kind.ASSISTANT_MESSAGE,
                {"text": reply, "tools_used": tools_used, "degraded": True},
            )
            return TurnResult(
                conversation_id=conversation_id,
                reply=reply,
                provider=provider_name,
                model=model,
                tools_used=tools_used,
                pending_plan=plan_brief(outstanding) if outstanding else None,
                deterministic=True,
            )
        status_code = getattr(exc, "status_hint", 502) or 502
        # Keep codes in a sensible HTTP range.
        if status_code not in (400, 401, 403, 429, 502, 503):
            status_code = 502
        return TurnResult(
            conversation_id=conversation_id,
            provider=provider_name,
            model=model,
            tools_used=tools_used,
            error={
                "detail": str(exc),
                "code": "PROVIDER_ERROR",
                "status_hint": status_code,
            },
        )

    _persist_pending(landlord, conversation_id, pending_specs)
    outstanding = load_fresh_plan(landlord, conversation_id)
    if outstanding is not None and outstanding.operation == "preview_batch":
        reply = _batch_preview_reply(outstanding, excluded_preview_errors)
    else:
        reply = (
            turn.text.strip()
            or "I wasn't able to produce an answer — try rephrasing."
        )
    response_deterministic = False
    if outstanding is None and _looks_like_confirmation_request(reply):
        # A model cannot create a confirmation contract with prose. If no
        # persisted plan backs its "reply yes", replace that claim with the
        # real blocker collected from the write tools (or an honest failure).
        response_deterministic = True
        if unresolved_write_inputs:
            reply = "I couldn't prepare an executable preview yet:\n" + "\n".join(
                f"• {question}" for question in unresolved_write_inputs
            )
        elif excluded_preview_errors:
            reply = "I couldn't prepare an executable preview:\n" + "\n".join(
                f"• {item['target']}: {item['error']}"
                for item in excluded_preview_errors
            )
        else:
            reply = (
                "I couldn't prepare an executable plan, so nothing is waiting "
                "for confirmation. Please resend the changes; I will only ask "
                "for Yes after the complete plan is saved."
            )
    audit(
        RamaAudit.Kind.ASSISTANT_MESSAGE,
        {
            "text": reply,
            "tools_used": tools_used,
            **({"deterministic": True} if response_deterministic else {}),
        },
    )

    return TurnResult(
        conversation_id=conversation_id,
        reply=reply,
        provider=provider_name,
        model=model,
        tools_used=tools_used,
        attachments=turn_attachments,
        pending_plan=plan_brief(outstanding) if outstanding else None,
        deterministic=response_deterministic,
    )
