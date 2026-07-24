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
    return str((reply.content or {}).get("text") or "") if reply else ""


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


def _preview_reply(tool: str, result: dict) -> str:
    preview = result.get("preview") or {}
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


def _persist_pending(
    landlord,
    conversation_id,
    pending_specs: list[dict] | None,
) -> None:
    """End-of-turn invariant: a plan row exists IFF something is outstanding.

    - Every preview produced this turn is persisted in call order as one plan
      (replacing any older one).
    - No preview: an unstarted plan from an earlier turn is treated as a
      change of subject and cleared — but a plan PAUSED mid-execution
      (awaiting a step's own confirm) survives an interjected question.
    """
    if not pending_specs:
        plan = load_fresh_plan(landlord, conversation_id)
        if plan is not None and plan.status != RamaPendingPlan.Status.AWAITING_STEP_CONFIRM:
            clear_plan(landlord, conversation_id)
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

    # Deterministic confirm state machine: on yes/no the backend itself runs
    # or cancels the previewed PLAN (single writes are one-step plans) — the
    # model never reconstructs tool calls (that was the endless re-preview
    # loop). Lease terminations and similar own-confirm steps pause execution
    # for their own explicit "yes" (tiered confirm).
    plan_progress: dict | None = None
    deterministic_reply: str | None = None
    pending_plan = load_fresh_plan(landlord, conversation_id)
    if pending_plan is not None and _is_affirmative(message):

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
            {"text": deterministic_reply, "tools_used": tools_used, "deterministic": True},
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
    audit(
        RamaAudit.Kind.ASSISTANT_MESSAGE,
        {"text": reply, "tools_used": tools_used},
    )

    return TurnResult(
        conversation_id=conversation_id,
        reply=reply,
        provider=provider_name,
        model=model,
        tools_used=tools_used,
        attachments=turn_attachments,
        pending_plan=plan_brief(outstanding) if outstanding else None,
    )
