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
import logging
import re
import uuid
from dataclasses import dataclass
from dataclasses import field
from datetime import datetime
from datetime import timedelta

from django.conf import settings
from django.utils import timezone

from . import autonomy as autonomy_policy
from . import memory
from .capabilities import select_tool_schemas
from .capabilities import supported_tool_for_request
from .models import RamaAudit
from .models import RamaPendingPlan
from .plan_runner import PENDING_PLAN_TTL_SECONDS
from .plan_runner import clear_plan
from .plan_runner import load_fresh_plan
from .plan_runner import plan_brief
from .plan_runner import plan_to_payload
from .plan_runner import run_plan
from .plan_runner import save_batch
from .plan_runner import save_single
from .plan_runner import validate_plan
from .providers import ProviderError
from .providers import Turn
from .providers import get_provider
from .registry import REGISTRY
from .registry import execute
from .roles import DELEGATION_TOOL_NAMES
from .roles import ROLE_PROMPTS
from .roles import SUB_TURN_MAX_ROUNDS
from .roles import role_allows_tool
from .roles import role_context
from .roles import role_tool_schemas
from .runtime import get_role_config
from .tool_meta import already_done_for

logger = logging.getLogger(__name__)
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


# "Undo" reverses something RAMA already did on its own. Deterministic for the
# same reason yes/no is: reversing a write must never depend on a weak model
# choosing to call the right tool with the right arguments.
_UNDO_EXACT = {
    "undo", "undo that", "undo it", "revert", "revert that", "revert it",
    "put it back", "put that back", "unde that", "roll that back", "roll back",
    "take that back", "reverse that", "undo the last one", "undo last",
}


def _is_undo_request(text: str) -> bool:
    return _norm_affirm(text) in _UNDO_EXACT


# "Remember that X" / "from now on X" — routed without asking the model,
# for the same reason the rename and house-layout intents are: a weak model
# picking `remember` out of a 100-tool schema is exactly the coin-flip the
# deterministic layer exists to remove. Only explicit imperatives match; an
# offhand "I never do Sundays" is left to the model.
_MEMORY_LEAD = re.compile(
    r"^(?:please\s+)?(?:remember|note|keep in mind|don'?t forget)\s+"
    r"(?:that\s+|this[:,]?\s+)?(?P<fact>.+)$",
    re.IGNORECASE,
)
_MEMORY_STANDING = re.compile(
    r"^(?P<fact>(?:from now on|going forward|as a rule)\b.+)$", re.IGNORECASE,
)
_MEMORY_FORGET = re.compile(
    r"^(?:please\s+)?forget\s+(?:that\s+|about\s+|the\s+)?(?P<subject>.+)$",
    re.IGNORECASE,
)
# Words that make a sentence a lookup, not a preference — "remember what the
# rent is?" must stay a question.
_MEMORY_QUESTION = re.compile(
    r"^(what|which|who|when|where|why|how|do you|did you|can you|could you)\b",
    re.IGNORECASE,
)

_STOPWORDS = {
    "the", "a", "an", "my", "our", "your", "their", "his", "her", "its",
    "i", "we", "you", "they", "he", "she", "it", "me", "us", "them",
    "is", "are", "was", "were", "be", "been", "am", "do", "does", "did",
    "to", "for", "of", "on", "in", "at", "by", "with", "from", "and", "or",
    "that", "this", "these", "those", "not", "never", "always", "prefer",
    "prefers", "want", "wants", "like", "likes", "should", "must", "will",
    "would", "can", "could", "go", "goes", "get", "gets", "have", "has",
}


def _memory_subject(fact: str) -> str:
    """A stable one-or-two-word label for what a preference is about.

    Deterministic so the SAME topic stated twice supersedes rather than
    accumulating two contradictory memories — the DB constraint enforces one
    active row per subject, and this is what makes the subject repeatable.
    """
    words = [w for w in re.findall(r"[a-z0-9']+", (fact or "").casefold())]
    salient = [w for w in words if w not in _STOPWORDS and len(w) > 2]
    return "-".join(salient[:2]) if salient else "-".join(words[:2])


def _memory_intent(message: str) -> dict | None:
    """(tool, arguments) for an explicit remember/forget instruction, or None."""
    text = (message or "").strip().rstrip(".!")
    if not text or _MEMORY_QUESTION.match(text) or text.endswith("?"):
        return None

    forget_match = _MEMORY_FORGET.match(text)
    if forget_match:
        subject = forget_match.group("subject").strip()
        # "forget it" is a cancellation, handled by _is_negative — not a memory
        # operation. _is_negative runs first, but be explicit.
        if subject.casefold() in {"it", "that", "this"}:
            return None
        return {"tool": "forget", "arguments": {"subject": subject}}

    match = _MEMORY_LEAD.match(text) or _MEMORY_STANDING.match(text)
    if not match:
        return None
    fact = match.group("fact").strip()
    if len(fact) < 4:
        return None
    return {
        "tool": "remember",
        "arguments": {"subject": _memory_subject(fact), "fact": fact},
    }


def _undo_hint(auto_executed: list[dict]) -> str:
    """The line that makes an unattended action recoverable.

    An auto-executed write with no visible way back is the failure mode that
    would make the whole tier feel unsafe, so this is not optional decoration.
    """
    if not any(item.get("undoable") for item in auto_executed):
        return ""
    return '\n\n(I did that automatically under your Constitution — say "undo" to reverse it.)'


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
    for key in ("property", "lease", "group", "work_order", "expense"):
        obj = result.get(key)
        if isinstance(obj, dict):
            return str(
                obj.get("name")
                or obj.get("lease_number")
                or obj.get("scope")
                or obj.get("id")
                or "",
            ).strip()
    return ""


def _write_result_message(tool: str, result: dict, target: str = "") -> str:
    message = str(result.get("message") or result.get("note") or "").strip()
    if message:
        return message
    if tool == "create_expense" and result.get("created"):
        exp = result.get("expense") if isinstance(result.get("expense"), dict) else {}
        amount = exp.get("amount") or result.get("amount") or ""
        scope = exp.get("scope") or exp.get("property") or target or "portfolio"
        desc = (exp.get("description") or "").strip()
        paid = exp.get("paid_on")
        bits = [f"Logged ${amount} expense"]
        if desc:
            bits.append(f"“{desc[:80]}”")
        bits.append(f"at {scope}")
        if paid:
            bits.append(f"(paid {paid})")
        return " ".join(bits) + "."
    if tool == "void_ledger_entry" and result.get("voided"):
        return str(
            result.get("message")
            or f"Voided {result.get('count') or 1} ledger entry(ies)."
        )
    if tool == "schedule_viewing" and result.get("created"):
        appt = result.get("appointment") if isinstance(result.get("appointment"), dict) else {}
        parts = [
            f"Scheduled viewing of {appt.get('property') or 'the listing'}",
            f"at {appt.get('starts_at') or 'the chosen time'}",
        ]
        if appt.get("contact_name"):
            parts.append(f"for {appt['contact_name']}")
        msg = " ".join(parts) + "."
        notified = result.get("notified") or {}
        if appt.get("contact_email"):
            msg += f" Email invite to {appt['contact_email']} is queued."
        elif notified.get("channels"):
            msg += f" Notify channels: {', '.join(notified.get('channels') or [])}."
        if result.get("calendar_link"):
            msg += f" Calendar: {result['calendar_link']}"
        return msg
    if tool == "update_property":
        before = str(result.get("previous_name") or target or "").strip()
        prop = result.get("property") or {}
        after = str(prop.get("name") if isinstance(prop, dict) else prop).strip()
        if before and after and before != after:
            return f"Renamed {before} to {after}."
        if after:
            return f"Updated listing {after}."
    lease_number = str(result.get("lease_number") or "").strip()
    prop_name = str(result.get("property") or "").strip()
    if result.get("updated") and lease_number:
        applied = result.get("applied") or []
        detail = f" ({', '.join(str(a) for a in applied)})" if applied else ""
        if prop_name:
            return f"Updated lease {lease_number} for {prop_name}{detail}."
        return f"Updated lease {lease_number}{detail}."
    label = _write_label(result) or target
    # "Created 950 McKenzie Ave — whole property" is wrong for money writes.
    if tool == "create_expense" and result.get("created"):
        return f"Logged expense at {label or 'portfolio'}."
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
            ),
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
            "own confirmation — reply yes to run it, or no to stop here.",
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
            ).order_by("created_at")[:3],
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
    """Map “where do I check X in the UI?” to a real nav destination.

    Never invent menu names. There is no “Appointments” item — viewings live
    under Calendar. Also answer “how do I check viewings via UI?” without
    requiring the word “dashboard”.
    """
    text = " ".join((message or "").casefold().split())
    # "Make a viewing… and send her an email" is scheduling, not nav.
    # The word "send" alone used to force Calendar link and skip schedule_viewing.
    if re.search(
        r"\b(schedule|book|make|create|arrange|set up)\b.+\b"
        r"(viewing|showing|appointment|tour)\b"
        r"|\b(viewing|showing)\b.+\b(tomorrow|today|at \d|\d\s*pm|\d\s*am)\b"
        r"|\b(viewing|showing)\b.+\bfor\b.+\b@\b",
        text,
    ) or re.search(
        r"\b(viewing|showing)\b.+\b(send|email|invite)\b"
        r"|\b(send|email|invite)\b.+\b(viewing|showing)\b",
        text,
    ):
        return None
    wants_nav = bool(
        re.search(
            r"\b(link|open|view|show|go|take|send|where|check|find|see)\b",
            text,
        )
    )
    if not wants_nav:
        return None
    for pattern, collection in (
        (r"\bproperty groups?\b", "property_groups"),
        (r"\bproperties\b|\blistings\b", "properties"),
        (r"\bdocuments?\b", "documents"),
        (r"\bleases?\b", "leases"),
        (r"\bfinances?\b|\bfinancial\b", "finances"),
        (r"\bmaintenance\b|\brepairs?\b", "maintenance"),
        (
            r"\bcalendar\b|\bappointments?\b|\bviewings?\b|\bshowings?\b|\bvisits?\b",
            "calendar",
        ),
        (r"\binquir(?:y|ies)\b|\bleads?\b", "inquiries"),
        (r"\bmessages?\b", "messages"),
        (r"\bsettings?\b", "settings"),
    ):
        if re.search(pattern, text):
            # "how do I check viewings" has no "dashboard" word — still answer.
            if collection == "calendar" or "dashboard" in text or "ui" in text:
                return collection
    if "dashboard" in text:
        return "dashboard"
    return None


_MONTHS = {
    "jan": 1,
    "january": 1,
    "feb": 2,
    "february": 2,
    "mar": 3,
    "march": 3,
    "apr": 4,
    "april": 4,
    "may": 5,
    "jun": 6,
    "june": 6,
    "jul": 7,
    "july": 7,
    "aug": 8,
    "august": 8,
    "sep": 9,
    "sept": 9,
    "september": 9,
    "oct": 10,
    "october": 10,
    "nov": 11,
    "november": 11,
    "dec": 12,
    "december": 12,
}


def _parse_clock_fragment(hour_s: str, minute_s: str | None, ampm: str | None) -> tuple[int, int]:
    hour = int(hour_s)
    minute = int(minute_s or 0)
    ap = (ampm or "").lower()
    if ap == "pm" and hour < 12:
        hour += 12
    elif ap == "am" and hour == 12:
        hour = 0
    return hour, minute


def _relative_when_from_text(text: str) -> str:
    """Parse 'tomorrow at 3 pm' / 'july 31st 15:00' → 'YYYY-MM-DD HH:MM' (Vancouver)."""
    from datetime import date
    from datetime import datetime
    from datetime import timedelta
    from zoneinfo import ZoneInfo

    tz = ZoneInfo("America/Vancouver")
    now = datetime.now(tz)
    day = now.date()
    low = (text or "").casefold()
    mdate = re.search(r"\b(20\d{2}-\d{2}-\d{2})\b", text or "")
    mdy = re.search(
        r"\b(jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|"
        r"jul(?:y)?|aug(?:ust)?|sep(?:t(?:ember)?)?|oct(?:ober)?|nov(?:ember)?|"
        r"dec(?:ember)?)\s+(\d{1,2})(?:st|nd|rd|th)?(?:\s*,?\s*(20\d{2}))?\b",
        low,
    )
    if mdate:
        day = datetime.strptime(mdate.group(1), "%Y-%m-%d").date()
    elif mdy:
        key = mdy.group(1)
        month = _MONTHS.get(key) or _MONTHS.get(key[:3])
        year = int(mdy.group(3) or now.year)
        day = date(year, int(month), int(mdy.group(2)))
    elif re.search(r"\btomorrow\b", low):
        day = day + timedelta(days=1)
    elif re.search(r"\btoday\b", low):
        day = day

    hour, minute = 15, 0
    # Range "15:00 - 15:30" or "3-3:30pm" → use start clock.
    range_m = re.search(
        r"\b(\d{1,2})(?::(\d{2}))?\s*(am|pm)?\s*[-–—to]+\s*"
        r"(\d{1,2})(?::(\d{2}))?\s*(am|pm)?\b",
        text or "",
        re.IGNORECASE,
    )
    if range_m:
        ap = range_m.group(3) or range_m.group(6)
        hour, minute = _parse_clock_fragment(
            range_m.group(1), range_m.group(2), ap
        )
    else:
        tm = re.search(
            r"\b(?:at\s+)?(\d{1,2})(?::(\d{2}))?\s*(am|pm)\b",
            text or "",
            re.IGNORECASE,
        )
        if not tm:
            tm = re.search(
                r"\bat\s+(\d{1,2})(?::(\d{2}))?\b",
                text or "",
                re.IGNORECASE,
            )
        if not tm:
            tm = re.search(r"\b(\d{1,2}):(\d{2})\b", text or "")
        if tm:
            ap = tm.group(3) if tm.lastindex and tm.lastindex >= 3 else None
            hour, minute = _parse_clock_fragment(tm.group(1), tm.group(2), ap)
    return f"{day.isoformat()} {hour:02d}:{minute:02d}"


def _duration_minutes_from_text(text: str, default: int = 30) -> int:
    """Parse '15:00 - 15:30' or 'for 30 minutes' → duration minutes."""
    range_m = re.search(
        r"\b(\d{1,2})(?::(\d{2}))?\s*(am|pm)?\s*[-–—to]+\s*"
        r"(\d{1,2})(?::(\d{2}))?\s*(am|pm)?\b",
        text or "",
        re.IGNORECASE,
    )
    if range_m:
        ap_end = range_m.group(6) or range_m.group(3)
        ap_start = range_m.group(3) or range_m.group(6)
        sh, sm = _parse_clock_fragment(range_m.group(1), range_m.group(2), ap_start)
        eh, em = _parse_clock_fragment(range_m.group(4), range_m.group(5), ap_end)
        start_m = sh * 60 + sm
        end_m = eh * 60 + em
        if end_m > start_m:
            return end_m - start_m
    dm = re.search(r"\b(\d{1,3})\s*(min|mins|minutes)\b", text or "", re.I)
    if dm:
        return max(5, min(240, int(dm.group(1))))
    return default


def _amend_pending_schedule_from_message(
    landlord, conversation_id, pending_plan, message: str
) -> str | None:
    """When a schedule_viewing plan is open, date/time corrections replace it.

    Bare Yes must never re-run an outdated Aug-1 preview after the landlord
    said "july 31 15:00-15:30".
    """
    if pending_plan is None:
        return None
    steps = list(pending_plan.steps.order_by("order"))
    if not steps or steps[0].tool != "schedule_viewing":
        return None
    text = (message or "").strip()
    if not text or _is_affirmative(text) and len(text) < 12:
        return None
    # Must look like a time/date correction, not an unrelated message.
    low = text.casefold()
    if not re.search(
        r"\b(today|tomorrow|january|february|march|april|may|june|july|august|"
        r"september|october|november|december|jan|feb|mar|apr|jun|jul|aug|sep|"
        r"oct|nov|dec|\d{1,2}:\d{2}|\d{1,2}\s*(am|pm)|should be|change|"
        r"instead|move|reschedule)\b",
        low,
    ):
        return None

    args = dict(steps[0].arguments or {})
    args["when"] = _relative_when_from_text(text)
    args["duration_minutes"] = str(_duration_minutes_from_text(text))
    # Preserve prospect contact from the original preview unless re-supplied.
    email_m = re.search(
        r"([A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,})",
        text,
    )
    if email_m:
        args["contact_email"] = email_m.group(1)
    name_m = re.search(
        r"\bfor\s+([A-Z][a-zA-Z'’-]+(?:\s+[A-Z][a-zA-Z'’-]+)+)",
        text,
    )
    if name_m and "@" not in name_m.group(1):
        args["contact_name"] = name_m.group(1).strip()

    result = execute("schedule_viewing", args, landlord=landlord)
    if result.get("error"):
        return str(result["error"])
    if result.get("needs_confirm"):
        save_single(landlord, conversation_id, "schedule_viewing", args)
        return _preview_reply("schedule_viewing", result)
    return _write_result_message("schedule_viewing", result)


def _schedule_viewing_intent(message: str) -> dict | None:
    """'Make a viewing for Garden Suite tomorrow 3pm for Name email@…'."""
    text = message or ""
    low = text.casefold()
    if not re.search(r"\b(viewing|showing)\b", low):
        return None
    if not (
        re.search(r"\b(schedule|book|make|create|arrange|set up)\b", low)
        or re.search(r"\bviewing for\b", low)
        or (
            re.search(r"\b(viewing|showing)\b", low)
            and re.search(r"\b(tomorrow|today|at \d|\d\s*pm)\b", low)
        )
    ):
        return None
    # Pure "where do I see viewings" is not scheduling.
    if re.search(
        r"\b(where|check|find|see|open|show)\b.+\b(viewings?|showings?|calendar)\b",
        low,
    ) and not re.search(r"\b(schedule|book|make|create|for)\b", low):
        return None

    email_m = re.search(
        r"([A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,})",
        text,
    )
    contact_email = email_m.group(1) if email_m else ""
    name = ""
    name_m = re.search(
        r"\bfor\s+([A-Z][a-zA-Z'’-]+(?:\s+[A-Z][a-zA-Z'’-]+)+)",
        text,
    )
    if name_m:
        name = name_m.group(1).strip()
        # Don't capture email local-part as name.
        if "@" in name:
            name = ""

    prop_q = ""
    if re.search(r"garden suite", low):
        # Exact listing name — do not append street (resolver is name-based).
        prop_q = "Garden Suite"
    elif re.search(r"\broom\s+([a-z0-9]+)\b", low):
        m = re.search(r"\broom\s+([a-z0-9]+)\b", low)
        prop_q = f"Room {m.group(1).upper()}" if m else ""
    if not prop_q and re.search(r"mckenzie", low):
        prop_q = "950 McKenzie"

    when = _relative_when_from_text(text)
    duration = _duration_minutes_from_text(text)
    if not prop_q:
        return None
    return {
        "tool": "schedule_viewing",
        "arguments": {
            "property_query": prop_q,
            "when": when,
            "duration_minutes": str(duration),
            "contact_name": name,
            "contact_email": contact_email,
            "notes": "",
        },
    }


def _show_all_rooms_intent(message: str) -> bool:
    text = " ".join((message or "").casefold().split())
    return bool(
        re.search(r"\b(show|list|view)\b", text)
        and re.search(r"\b(all|every|my)\b", text)
        and re.search(r"\brooms?\b", text),
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


def _mentioned_room_names(text: str) -> list[str]:
    """Extract explicit room labels without inventing sequential letters."""
    matches: list[tuple[int, str, str]] = []
    pattern = re.compile(
        r"\b(?P<kind>bonus\s+room|private\s+room|bedroom|room)"
        r"\s+(?:named\s+|called\s+)?"
        r"(?:(?P<quote>[\"'])\s*(?P<quoted>[^\"']+?)\s*(?P=quote)"
        r"|(?P<label>[A-Za-z0-9][A-Za-z0-9_-]{0,30}))",
        flags=re.IGNORECASE,
    )
    for match in pattern.finditer(text or ""):
        label = " ".join(
            str(match.group("quoted") or match.group("label") or "").split(),
        )
        if not label or label.casefold() in {"in", "inside", "into", "to"}:
            continue
        kind = match.group("kind").casefold()
        name = (
            f"Bonus room {label}"
            if kind.startswith("bonus")
            else f"Room {label}"
        )
        matches.append((match.start(), name, name.casefold()))
    seen: set[str] = set()
    ordered: list[str] = []
    for _position, name, key in sorted(matches):
        if key not in seen:
            ordered.append(name)
            seen.add(key)
    return ordered


def _mentioned_shared_areas(text: str) -> list[dict]:
    lowered = (text or "").casefold()
    specs: list[dict] = []
    aliases = (
        (("washroom", "bathroom"), "Washroom", "BATHROOM"),
        (("kitchen",), "Kitchen", "KITCHEN"),
        (("patio",), "Patio", "BALCONY"),
        (("balcony",), "Balcony", "BALCONY"),
        (("living room",), "Living room", "LIVING_ROOM"),
        (("entry room", "entryway"), "Entryway", "HALLWAY"),
        (("laundry",), "Laundry", "LAUNDRY"),
    )
    used_types: set[str] = set()
    landlord_shares = bool(
        re.search(
            r"\b(?:landlord|owner|landlord'?s (?:son|daughter|children|family))"
            r".{0,50}\b(?:share|shares|use|uses|live|lives)\b"
            r"|\bshared?\s+with\s+(?:the\s+)?(?:landlord|owner)",
            lowered,
        ),
    )
    for words, name, area_type in aliases:
        if area_type in used_types or not any(
            re.search(rf"\b{re.escape(word)}s?\b", lowered) for word in words
        ):
            continue
        used_types.add(area_type)
        specs.append(
            {
                "name": name,
                "area_type": area_type,
                "count": 1,
                "shared_with_landlord": landlord_shares,
            },
        )
    return specs


def _unit_phrase_from_message(message: str) -> str:
    """Pull the suite/unit target phrase out of a convert/add-rooms request."""
    text = message or ""
    patterns = (
        r"\b(?:into|inside|in|to)\s+(?:the\s+)?(?P<target>[^?.!,;]+?)(?=\s+that\b|\s+which\b|\s+it\b|[?.!,;]|$)",
        r"\b(?:convert|turn)\s+(?:the\s+)?(?P<target>[^?.!,;]+?)"
        r"(?:\s+(?:into|to)\s+(?:rooms?|room[- ]by[- ]room|a property group))?",
        r"\b(?:change how)\s+(?:the\s+)?(?P<target>[^?.!,;]+?)\s+(?:is\s+)?rented",
    )
    for pattern in patterns:
        match = re.search(pattern, text, flags=re.IGNORECASE)
        if match:
            phrase = " ".join(match.group("target").split()).strip(" \"'")
            # Drop trailing filler the landlord used as description, not name.
            phrase = re.sub(
                r"\b(?:has|with|and then|and also)\b.*$",
                "",
                phrase,
                flags=re.IGNORECASE,
            ).strip(" ,.-")
            if phrase and len(phrase) <= 120:
                return phrase
    return ""


def _score_unit_for_message(unit, message: str) -> int:
    """Address-aware unit ranking for deterministic suite routing."""
    from .unit_structure import _HOLDING_NOISE
    from .unit_structure import _tokenise
    from .unit_structure import _unit_match_score

    phrase = _unit_phrase_from_message(message)
    score = 0
    if phrase:
        score = max(score, _unit_match_score(unit, phrase))
    # Whole-message scoring catches "mckenzie ave garden suite" even when the
    # capture group is messy ("the mckenzie ave garden suite that suite has…").
    score = max(score, _unit_match_score(unit, message))
    lowered = (message or "").casefold()
    for offering in unit.offerings.all():
        name = (offering.name or "").casefold()
        if name and name in lowered:
            score = max(score, 90 + min(len(name), 30))
    # Strong boost when street tokens + unit name both appear (breaks twin
    # "Garden Suite" rows without another round of yes-loops).
    msg_tokens = _tokenise(message) - _HOLDING_NOISE
    holding = unit.holding
    if holding is not None:
        addr_hits = (
            (_tokenise(holding.address) | _tokenise(holding.name)) - _HOLDING_NOISE
        ) & msg_tokens
        unit_hits = (_tokenise(unit.name) - _HOLDING_NOISE) & msg_tokens
        if addr_hits and unit_hits:
            score += 40 + min(len(addr_hits) * 8, 32)
        elif addr_hits:
            score += 12 + min(len(addr_hits) * 5, 20)
    return score


def _resolve_unit_from_message(landlord, message: str):
    """Pick one physical unit for a convert/add-rooms request.

    Returns (unit|None, disambiguation_dict|None). When multiple same-named
    suites exist, street evidence in the message must decide; if it cannot,
    return candidates so the caller can ask — never invent a second group.
    """
    from rentium.properties.models import Property
    from rentium.properties.models import PropertyUnit

    from .unit_structure import _resolve_unit

    units = list(
        PropertyUnit.objects.filter(landlord=landlord)
        .select_related("holding")
        .prefetch_related("offerings"),
    )
    ranked = [
        (score, unit)
        for unit in units
        if (score := _score_unit_for_message(unit, message)) > 0
    ]
    ranked.sort(key=lambda row: (-row[0], str(row[1].pk)))
    if ranked:
        best = ranked[0][0]
        top = [unit for score, unit in ranked if score == best]
        if len(top) == 1:
            return top[0], None
        if len(ranked) > 1 and best - ranked[1][0] >= 15:
            return ranked[0][1], None
        return None, {
            "error": "Several units match — which one?",
            "candidates": [
                f"{u.name} ({u.holding.address or u.holding.name})"
                for _score, u in ranked[:6]
            ],
            "question_for_user": (
                "I found more than one matching unit. Which one do you mean?\n"
                + "\n".join(
                    f"• {u.name} at {u.holding.address or u.holding.name}"
                    for _score, u in ranked[:6]
                )
            ),
        }

    # Fall back to free-text resolve on the captured target phrase, then to
    # complete-unit listing names (older portfolios sometimes lack unit rows
    # that match the landlord's wording but still have the listing).
    phrase = _unit_phrase_from_message(message)
    if phrase:
        unit, err = _resolve_unit(landlord, phrase)
        if unit is not None:
            return unit, None
        if isinstance(err, dict) and err.get("candidates"):
            return None, {
                **err,
                "question_for_user": (
                    f"{err.get('error')}\n"
                    + "\n".join(f"• {c}" for c in err.get("candidates") or [])
                ),
            }

    lowered = (message or "").casefold()
    listings = list(
        Property.objects.filter(
            landlord=landlord,
            property_category=Property.PropertyCategory.COMPLETE_UNIT,
            unit__isnull=False,
        )
        .select_related("unit", "unit__holding", "holding")
        .prefetch_related("unit__offerings"),
    )
    listing_hits = [
        prop
        for prop in listings
        if prop.name and prop.name.casefold() in lowered
    ]
    listing_hits.sort(key=lambda prop: (-len(prop.name), prop.pk))
    if len(listing_hits) == 1 and listing_hits[0].unit_id:
        return listing_hits[0].unit, None
    if len(listing_hits) > 1:
        # Prefer the listing whose holding address also appears in the message.
        from .unit_structure import _tokenise, _HOLDING_NOISE

        msg_tokens = _tokenise(message) - _HOLDING_NOISE
        scored = []
        for prop in listing_hits:
            holding = prop.unit.holding if prop.unit_id else prop.holding
            hits = (
                (_tokenise(holding.name if holding else "")
                 | _tokenise(holding.address if holding else ""))
                - _HOLDING_NOISE
            ) & msg_tokens
            scored.append((len(hits), prop))
        scored.sort(key=lambda row: (-row[0], row[1].pk))
        if scored and scored[0][0] > 0 and (
            len(scored) == 1 or scored[0][0] > scored[1][0]
        ):
            return scored[0][1].unit, None
        return None, {
            "error": "Several complete-unit listings match — which one?",
            "candidates": [
                f"{p.name} ({(p.unit.holding.address if p.unit_id else p.address) or p.name})"
                for p in listing_hits[:6]
            ],
            "question_for_user": (
                "I found more than one matching suite listing. Which one?\n"
                + "\n".join(
                    f"• {p.name} at "
                    f"{(p.unit.holding.address if p.unit_id else p.address) or 'unknown address'}"
                    for p in listing_hits[:6]
                )
            ),
        }
    return None, None


def _unit_room_layout_intent(landlord, message: str) -> dict | None:
    """High-confidence suite-to-rooms request with its full stated layout.

    This is the path that must own "add rooms into the garden suite" and
    "convert this suite to rent by room". Falling through to the model is what
    produced invented room letters, fake property groups, and wrong links.
    """
    lowered = (message or "").casefold()
    if not re.search(
        r"\b(?:add|create|make|turn|convert|divide|split|change how)\b",
        lowered,
    ):
        return None
    # "add rooms into suite" / "convert suite to rooms" / "rent by room"
    is_convert = bool(
        re.search(
            r"\b(?:turn|convert|divide|split)\b.+\b(?:room|rooms|by[- ]room|property group)\b"
            r"|\bchange how\b.+\brented\b"
            r"|\brent\b.+\b(?:by room|room by room|room-by-room)\b"
            r"|\b(?:suite|unit|floor)\b.+\b(?:into|as)\b.+\brooms?\b",
            lowered,
        )
    )
    is_add_rooms = bool(
        re.search(r"\b(?:add|create|make)\b", lowered)
        and re.search(r"\brooms?\b", lowered)
        and re.search(r"\b(?:suite|unit|floor|into|inside)\b", lowered)
    )
    if not (is_convert or is_add_rooms):
        return None

    room_names = _mentioned_room_names(message)
    shared_areas = _mentioned_shared_areas(message)
    unit, ambiguity = _resolve_unit_from_message(landlord, message)

    if unit is None and ambiguity:
        return {
            "tool": None,
            "deterministic_reply": ambiguity.get("question_for_user")
            or ambiguity.get("error"),
        }
    if unit is None:
        return None

    # Landlord asked to convert but did not name the rooms yet — ask once,
    # keep the unit pinned, do not invent L/M.
    if len(room_names) < 1 and is_convert:
        label = f"{unit.name} at {unit.holding.address or unit.holding.name}"
        return {
            "tool": None,
            "deterministic_reply": (
                f"I'll convert {label} from a complete unit to room-by-room "
                "rentals (property group + one listing per room). What should "
                "each rentable room be called? Example: Bonus room J and Room K. "
                "Also list any shared areas (kitchen, washroom, patio) if you "
                "haven't already."
            ),
        }
    if len(room_names) < 2 and is_add_rooms and not is_convert:
        # "add two rooms" without names is incomplete — ask, don't invent.
        if len(room_names) < 1:
            label = f"{unit.name} at {unit.holding.address or unit.holding.name}"
            return {
                "tool": None,
                "deterministic_reply": (
                    f"I can turn {label} into room-by-room rentals. What should "
                    "the rooms be named (e.g. Bonus room J, Room K), and which "
                    "shared areas does the suite have (kitchen, washroom, patio)?"
                ),
            }

    if len(room_names) < 1:
        return None

    from rentium.properties.models import Property

    attached_group = getattr(unit, "room_group", None)
    complete_listing = (
        unit.offerings.filter(
            property_category=Property.PropertyCategory.COMPLETE_UNIT,
        )
        .order_by("-is_active_offering", "created_at", "pk")
        .first()
    )
    group_name = (
        attached_group.name
        if attached_group is not None
        else (
            complete_listing.name
            if complete_listing is not None
            else f"{unit.holding.name} {unit.name}"
        )
    )
    return {
        "tool": "configure_unit_room_offerings",
        "arguments": {
            "unit_name": str(unit.pk),
            "room_names_json": json.dumps(room_names),
            "group_name": group_name,
            "shared_areas_json": json.dumps(shared_areas),
            "holding": unit.holding.address or unit.holding.name,
        },
    }


def _recent_media_manifest(landlord, conversation_id) -> dict | None:
    row = (
        RamaAudit.objects.filter(
            landlord=landlord,
            conversation_id=conversation_id,
            kind=RamaAudit.Kind.TOOL_CALL,
            content__tool="list_listing_media",
        )
        .order_by("-created_at")
        .first()
    )
    if row is None:
        return None
    result = row.content.get("result") or {}
    return result if result.get("listing_id") and result.get("media") else None


def _mentioned_media_property(landlord, message: str):
    from rentium.properties.models import Property

    lowered = (message or "").casefold()
    id_match = re.search(
        r"\b(?:property|listing)\s*#?\s*(\d+)\b",
        lowered,
    )
    if id_match:
        return Property.objects.filter(
            landlord=landlord,
            pk=id_match.group(1),
        ).first()
    matches = [
        prop
        for prop in Property.objects.filter(landlord=landlord)
        if prop.name and prop.name.casefold() in lowered
    ]
    if not matches:
        return None
    matches.sort(key=lambda prop: (-len(prop.name), prop.created_at, prop.pk))
    longest = len(matches[0].name)
    top = [prop for prop in matches if len(prop.name) == longest]
    return top[0] if len(top) == 1 else None


def _media_management_intent(landlord, conversation_id, message: str) -> dict | None:
    lowered = (message or "").casefold()
    if not re.search(r"\b(?:remove|delete|take off|get rid of)\b", lowered):
        return None
    if not re.search(r"\b(?:photo|photos|image|images|picture|pictures)\b", lowered):
        return None

    recent = _recent_media_manifest(landlord, conversation_id)
    direct_handles = re.findall(r"\bgallery:\d+\b|\bprimary\b", lowered)
    prop = _mentioned_media_property(landlord, message)
    if prop is None and recent is not None:
        from rentium.properties.models import Property

        prop = Property.objects.filter(
            landlord=landlord,
            pk=recent["listing_id"],
        ).first()

    handles = list(dict.fromkeys(direct_handles))
    if not handles and recent is not None:
        numbers = [
            int(value)
            for value in re.findall(r"(?:#|\b)(\d{1,2})(?=\b)", lowered)
        ]
        by_number = {
            int(row["selection_number"]): row["handle"]
            for row in recent.get("media") or []
            if row.get("selection_number")
        }
        handles = list(
            dict.fromkeys(
                by_number[number] for number in numbers if number in by_number
            ),
        )

    if prop is not None and handles:
        return {
            "tool": "remove_photos_from_listing",
            "arguments": {
                "property_query": str(prop.pk),
                "media_handles_json": json.dumps(handles),
            },
        }
    if prop is not None:
        return {
            "tool": "list_listing_media",
            "arguments": {"property_query": str(prop.pk)},
        }
    return None


def _media_manifest_reply(result: dict) -> str:
    media = result.get("media") or []
    if not media:
        return f"{result.get('listing') or 'That listing'} has no photos."
    lines = [f"Photos currently on {result.get('listing')}:"]
    for row in media:
        kind = "main photo" if row.get("kind") == "primary" else "gallery"
        caption = str(row.get("caption") or "").strip()
        detail = caption or row.get("filename") or row.get("handle")
        lines.append(
            f"{row.get('selection_number')}. {kind} — {detail}",
        )
    lines.append(
        "Select the exact thumbnail(s), or say “remove photos 2 and 4”. "
        "I’ll show one final preview before anything changes.",
    )
    return "\n".join(lines)


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
    if tool == "create_expense":
        amount = preview.get("amount") or ""
        desc = preview.get("description") or "expense"
        where = preview.get("property") or "portfolio"
        day = preview.get("effective_date") or "today"
        bank = preview.get("bank_status") or (
            f"paid {preview['paid_on']}"
            if preview.get("paid_on")
            else "not yet taken from bank"
        )
        cat = (preview.get("category") or "").replace("_", " ").title()
        lines = [
            "Expense to file (no receipt required):",
            f"• Amount: ${amount}",
            f"• Description: {desc}",
            f"• Property: {where}",
            f"• Date: {day}",
        ]
        if cat:
            lines.append(f"• Category: {cat}")
        lines.append(f"• Bank: {bank}")
        if preview.get("duplicate_warning"):
            lines.append(f"• Note: {preview['duplicate_warning']}")
        lines.append("Reply yes to post this expense, or no to cancel.")
        return "\n".join(lines)
    if tool == "void_ledger_entry":
        entries = preview.get("entries") or []
        lines = [
            f"Void {preview.get('count') or len(entries)} expense(s) "
            f"(reversal — originals stay for audit):",
        ]
        for e in entries[:10]:
            lines.append(
                f"• ${e.get('amount')} — {(e.get('description') or '')[:90]}"
            )
        if preview.get("reason"):
            lines.append(f"Reason: {preview['reason']}")
        lines.append("Reply yes to void, or no to cancel.")
        return "\n".join(lines)
    if tool == "schedule_viewing":
        when_line = preview.get("starts_at") or preview.get("when") or "—"
        if preview.get("ends_at"):
            when_line = f"{when_line} → {preview['ends_at']}"
        lines = [
            "Schedule viewing:",
            f"• Property: {preview.get('property') or '—'}",
            f"• When: {when_line}",
        ]
        if preview.get("contact_name"):
            lines.append(f"• Prospect: {preview['contact_name']}")
        if preview.get("contact_email"):
            lines.append(
                f"• Email invite to: {preview['contact_email']} "
                "(sent when you confirm)"
            )
        else:
            lines.append(
                "• No prospect email — add contact_email so we can notify them"
            )
        lines.append("Reply yes to schedule and email, or no to cancel.")
        return "\n".join(lines)
    if tool == "reschedule_viewing":
        lines = [
            "Reschedule viewing:",
            f"• Property: {preview.get('property') or '—'}",
            f"• From: {preview.get('from') or '—'}",
            f"• To: {preview.get('to') or '—'}",
        ]
        if preview.get("contact_name") or preview.get("contact_email"):
            lines.append(
                f"• Prospect: {preview.get('contact_name') or ''} "
                f"{preview.get('contact_email') or ''}".strip()
            )
        if preview.get("will_email_prospect"):
            lines.append("• Will email prospect the new time")
        lines.append("Reply yes to reschedule, or no to cancel.")
        return "\n".join(lines)
    if tool == "cancel_viewing":
        lines = [
            "Cancel viewing:",
            f"• Property: {preview.get('property') or preview.get('property_name') or '—'}",
            f"• When: {preview.get('starts_at') or preview.get('when') or '—'}",
        ]
        if preview.get("contact_name"):
            lines.append(f"• Prospect: {preview['contact_name']}")
        lines.append("Reply yes to cancel, or no to keep it.")
        return "\n".join(lines)
    if tool == "mark_ledger_paid":
        lines = [
            "Mark expense paid:",
            f"• Amount: ${preview.get('amount') or '—'}",
            f"• Description: {preview.get('description') or '—'}",
            f"• Paid on: {preview.get('paid_on') or 'today'}",
        ]
        lines.append("Reply yes to mark paid, or no to cancel.")
        return "\n".join(lines)
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
    if tool == "configure_unit_room_offerings":
        rooms = preview.get("rooms") or []
        room_lines = ", ".join(
            f"{row.get('name')} ({row.get('action')})" for row in rooms
        )
        areas = preview.get("shared_areas") or []
        area_lines = ", ".join(
            f"{row.get('name')} ({row.get('action')})" for row in areas
        )
        text = (
            f"Preview: convert {preview.get('unit')} at "
            f"{preview.get('holding')} from a complete unit into room-by-room "
            f"rentals under one property group.\n"
            f"Property group: {preview.get('group')} "
            f"({preview.get('group_action')}).\n"
            f"Room offerings (each gets its own listing): "
            f"{room_lines or 'none'}.\n"
            f"Shared areas kept on the group: {area_lines or 'none'}."
        )
        parked = preview.get("will_park") or []
        if parked:
            text += "\nParked (not deleted): " + ", ".join(parked) + "."
        revived = preview.get("will_reactivate_other_rooms") or []
        if revived:
            text += "\nExisting room offerings reactivated: " + ", ".join(revived) + "."
        text += (
            "\nAfter this, each room can have its own rent — say the amounts "
            "when you want them set. Reply yes to confirm, or no to cancel."
        )
        return text
    if tool == "remove_photos_from_listing":
        media = preview.get("media") or []
        labels = ", ".join(
            f"#{row.get('selection_number')} ({row.get('filename') or row.get('handle')})"
            for row in media
        )
        return (
            f"Preview: remove {len(media)} exact photo(s) from "
            f"{preview.get('listing')}: {labels}. No other photos will change.\n"
            "Reply yes to confirm, or no to cancel."
        )
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
        {"kind": "single", "tool": tool, "arguments": arguments},
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
        f"Preview — one “Yes” will run all {plan.steps.count()} changes:",
    ]
    for index, step in enumerate(plan.steps.order_by("order"), start=1):
        args = step.arguments or {}
        target = step.target_label or str(
            args.get("property_query") or args.get("name") or "item",
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
        ),
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
                        },
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
        str(enriched.get("group_name") or "").casefold().split(),
    )
    if not group_key:
        return enriched
    for candidate in _recent_creation_defaults(landlord, conversation_id):
        candidate_group = " ".join(
            str(candidate.get("group_name") or "").casefold().split(),
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
            )[:2],
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
        PropertyGroup.objects.filter(landlord=landlord).order_by("-name"),
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
                str(candidate.get("group_name") or "").casefold().split(),
            )
            candidate_address = " ".join(
                str(candidate.get("address") or "").casefold().split(),
            )
            if candidate_group == group_key and candidate_address == address_key:
                if candidate.get("city") and candidate.get("province"):
                    return candidate
        for candidate in defaults:
            candidate_address = " ".join(
                str(candidate.get("address") or "").casefold().split(),
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
            },
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
        ),
    )


# A first-person claim that a write ALREADY happened. Deliberately narrow: it
# must be the model saying it did the thing, not the ledger being described.
# "no layout recorded" and "the deposit is recorded as $425" are readings, not
# claims, and this must never fire on them.
_DID = (
    r"(recorded|created|added|posted|logged|saved|updated|deleted|removed"
    r"|cancelled|canceled|scheduled|voided|refunded|issued|sent|marked"
    r"|invited|terminated|renamed)"
)
# "recorded AS $425" describes the books; "recorded THE payment" claims a write.
_NOT_AN_ACTION = r"(?!\s+(?:as|in|under|on the ledger\b))"
_CLAIMED_WRITE = re.compile(
    # "...— I updated the rent": first person, anywhere in the sentence.
    r"\bi(?:'ve|’ve| have| just)?\s+" + _DID + r"\b" + _NOT_AN_ACTION
    # "Recorded the $100 payment...": bare past tense opening a sentence.
    + r"|(?:^|[.!?]\s+|\n\s*|—\s*)" + _DID + r"\b" + _NOT_AN_ACTION,
    re.IGNORECASE,
)
_NOT_A_CLAIM = re.compile(
    r"\b(?:not|never|no|nothing|isn't|isn’t|wasn't|wasn’t|hasn't|hasn’t"
    r"|haven't|haven’t|cannot|can't|can’t|won't|won’t|couldn't|couldn’t)\b",
    re.IGNORECASE,
)


def _sentences(text: str) -> list[str]:
    return [part for part in re.split(r"(?<=[.!?])\s+|\n+", text or "") if part.strip()]


def claims_completed_write(text: str) -> bool:
    """True when the model says, in its own voice, that it already wrote.

    Checked per SENTENCE so a negation elsewhere in a long reply cannot excuse
    a claim, and a claim elsewhere cannot condemn an honest "I haven't recorded
    that yet".
    """
    for sentence in _sentences(text):
        if _NOT_A_CLAIM.search(sentence):
            continue
        if "?" in sentence:  # "Shall I record it?" is an offer, not a claim
            continue
        if _CLAIMED_WRITE.search(sentence):
            return True
    return False


# A reply that ANNOUNCES work instead of reporting it. The landlord asked
# whether the $100 was in the ledger and got "I'll verify the deposit payment
# is recorded correctly. Checking the ledger now." — and the turn ended there.
# Twice. They had to reply "ok and?" and "why do u make me guess" to get an
# answer that the engine had already had the tools to produce.
#
# Narrow on purpose: it must fire on a turn that promised and stopped, and
# never on a turn that promised something genuinely LATER ("I'll check again
# tomorrow") or that already delivered alongside the promise.
_PROMISE = re.compile(
    r"\b(?:i'?ll|i will|let me|i'?m going to|i am going to|going to|about to)\s+"
    # Up to three filler words between the promise and the verb, so "I'll go
    # and check" and "I'll just quickly verify" are caught. Non-greedy and
    # bounded: unbounded, "I'll record the $100 payment ... " would eventually
    # reach some verb further down the sentence and condemn a normal preview.
    r"(?:[\w,.-]+\s+){0,3}?"
    r"(?:re-)?"
    r"(check|verify|look|confirm|review|pull up|find out|see|take a look|dig)\b"
    r"|\b(checking|verifying|looking|reviewing|pulling up|taking a look)\b",
    re.IGNORECASE,
)
# "I'll check tomorrow / next week / when they reply" is a real commitment
# about the future, not a stall — it must not be caught.
_LATER = re.compile(
    r"\b(tomorrow|next week|next month|later today|on monday|once |after |when )\b",
    re.IGNORECASE,
)


def promises_without_delivering(text: str, tools_used) -> bool:
    """True when the reply announces work it did not then do.

    Checked per SENTENCE, like claims_completed_write, so a promise in one
    clause is not excused by an answer elsewhere — and so an answer that merely
    CONTAINS the word "checking" is not condemned.

    `tools_used` is not sufficient on its own: the model may call a read tool
    and STILL reply "let me look into that", which is the same failure with a
    tool call in front of it. So the test is on what the landlord was told, and
    the remedy is to make the model finish the thought.
    """
    for sentence in _sentences(text):
        if _LATER.search(sentence):
            continue
        if "?" in sentence:  # "Shall I check?" is an offer, not a stall
            continue
        if _PROMISE.search(sentence):
            return True
    return False


# What a stalled turn is told to do instead. Deliberately imperative and
# specific about the shape of the answer, because "be more helpful" does not
# survive a weak model.
_DELIVER_NOW = (
    "You just told the landlord you would check something, and then stopped "
    "without telling them what you found. Do it NOW in this turn: call the "
    "tool you need, then state the result AND what it means for them, in the "
    "same message. Never announce that you are about to look something up — "
    "look it up and report it. If you cannot, say plainly what is blocking you."
)


def _capability_gap_hint(landlord, message: str, conversation_id) -> str:
    """What to say after refusing a fabricated claim.

    A bare "that didn't happen" leaves the landlord no better off, so this logs
    the gap (the same backlog the "learn now" flow reads) and says plainly what
    to do next.
    """
    try:
        execute(
            "log_capability_gap",
            {
                "request": (message or "")[:400],
                "note": "Model claimed this was done without calling any write tool.",
            },
            landlord=landlord,
        )
    except Exception:
        logger.exception("capability gap logging failed")
    return (
        "Either I have no tool for that yet, or I lost track of the step. Say "
        "it once more and I'll either show you a preview to confirm, or tell "
        "you straight that I can't do it."
    )


def _refuse_if_already_done(tool_name, arguments, result, landlord):
    """Replace a preview with a refusal when the write is already on the books.

    This is the FIRST of the two sites `already_done` runs at (the second is
    plan_runner.validate_plan, which covers the window between the landlord
    seeing a preview and confirming it). Here, so that a duplicate is never
    shown as a proposal at all — the landlord should not have to notice that
    what RAMA is offering to record already happened.

    Deliberately placed on the generic path rather than inside the three money
    tools that had hand-written checks: a new write tool inherits this by
    declaring `already_done`, and cannot forget to call it.
    """
    if not isinstance(result, dict) or not result.get("needs_confirm"):
        return None
    tool = REGISTRY.get(tool_name)
    if tool is None:
        return None
    allowed = set(tool.parameters.get("properties") or ())
    safe_args = {
        k: v for k, v in (arguments or {}).items() if k in allowed and k != "confirm"
    }
    detail = already_done_for(tool_name, landlord, **safe_args)
    if not detail:
        return None
    return {
        "already_done": True,
        "error": detail,
        "instruction": (
            "Do NOT offer to record this. Tell the landlord plainly that it is "
            "already on the books, and say which record holds it."
        ),
    }


def _is_write_result(result) -> bool:
    """Whether a tool result represents a change that actually landed.

    Keyed on the RESULT, not on the tool's signature. The previous version
    asked whether the tool takes a `confirm` — but every model-issued call has
    its `confirm` blanked before dispatch, so such a call returns a PREVIEW and
    writes nothing. That made "did this turn write?" answer yes for a turn that
    had only proposed, which is exactly the case the claimed-write guard exists
    to catch: preview `record_payment`, then say "Recorded the payment."
    """
    if not isinstance(result, dict):
        return False
    if result.get("needs_confirm") or result.get("error"):
        return False
    return bool(
        result.get("created")
        or result.get("updated")
        or result.get("deleted")
        or result.get("terminated")
        or result.get("done")
        or result.get("ok")
        or result.get("recorded"),
    )


def _turn_wrote_anything(turn_writes, auto_executed) -> bool:
    """Whether this turn actually changed anything.

    `turn_writes` is the list of tools whose RESULT said so — see
    _is_write_result. Auto-executed plans count because run_plan really did
    run them.
    """
    return bool(auto_executed) or bool(turn_writes)


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
    landlord, conversation_id, limit: int = 8, budget_chars: int = 2000,
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
        # Money writes report {ok/recorded, entry_id, amount, still_owing} and
        # matched none of the verbs above, so recording $100 left NO trace in
        # the next turn's prompt and the model denied it had happened. The
        # amount is carried because "a payment was recorded" without the figure
        # does not answer "is the $100 in?".
        elif (res.get("ok") or res.get("recorded")) and (
            res.get("entry_id") or res.get("subject")
        ):
            amount = res.get("amount")
            target = res.get("charge") or res.get("subject") or ""
            money = f"${amount} " if amount else ""
            owing = (
                f" ({res['still_owing']} still owing)"
                if res.get("still_owing") is not None
                else ""
            )
            notes.append(f"{tool}: recorded {money}{target}{owing}".strip())
    seen: set[str] = set()
    deduped: list[str] = []
    for n in notes:
        if n not in seen:
            seen.add(n)
            deduped.append(n)
    return deduped[-6:]


_REFERENT_WORDS = {
    "",
    "it",
    "this",
    "that",
    "this one",
    "that one",
    "the property",
    "that property",
    "this property",
    "the listing",
    "that listing",
    "this listing",
    "the unit",
    "that unit",
    "this unit",
    "the suite",
    "that suite",
    "this suite",
    "there",
}


def _conversation_focus(
    landlord,
    conversation_id,
    message: str,
    live_portfolio: dict,
) -> dict:
    """Resolve what short follow-ups such as "it" most likely refer to.

    This is conversation-scoped and grounded in live landlord-owned records.
    Explicit entity names in the current message win, then recent tool targets,
    then recent user messages. It is a routing hint, never authority to cross
    ownership boundaries; every called tool still performs its normal resolver.
    """
    listings = live_portfolio.get("listings") or []
    by_name = {
        str(row.get("name")).casefold(): row
        for row in listings
        if row.get("name")
    }

    def named_in(text: str):
        lowered = (text or "").casefold()
        matches = [
            (name, row) for name, row in by_name.items() if name and name in lowered
        ]
        if not matches:
            return None
        return max(matches, key=lambda item: len(item[0]))[1]

    row = named_in(message)
    source = "current message" if row else ""
    recent = RamaAudit.objects.filter(
        landlord=landlord,
        conversation_id=conversation_id,
    ).order_by("-created_at")[:24]
    if row is None:
        for audit_row in recent:
            content = audit_row.content or {}
            if audit_row.kind == RamaAudit.Kind.TOOL_CALL:
                args = content.get("arguments") or {}
                result = content.get("result") or {}
                candidates = [
                    args.get("property_query"),
                    args.get("property_name"),
                    (result.get("property") or {}).get("name")
                    if isinstance(result.get("property"), dict)
                    else result.get("property"),
                    (result.get("preview") or {}).get("property")
                    if isinstance(result.get("preview"), dict)
                    else None,
                ]
                for candidate in candidates:
                    match = by_name.get(str(candidate or "").casefold())
                    if match:
                        row = match
                        source = f"recent {content.get('tool') or 'tool'} target"
                        break
            elif audit_row.kind == RamaAudit.Kind.USER_MESSAGE:
                row = named_in(content.get("text", ""))
                if row:
                    source = "recent user message"
            if row:
                break
    if row is None:
        return {}
    return {
        "property": {
            "name": row.get("name"),
            "category": row.get("category"),
            "primary_type": row.get("primary_type"),
            "group": row.get("group"),
            "lease_number": row.get("lease_number"),
        },
        "source": source,
        "instruction": (
            "Resolve it/this/that/the property/the unit/the suite to this property "
            "unless the landlord explicitly names another entity. Follow-up questions "
            "inherit the topic but remain read-only."
        ),
    }


def _contextualize_tool_arguments(
    tool_name: str, arguments: dict | None, focus: dict,
) -> dict:
    """Replace pronoun placeholders with the grounded conversation target."""
    args = dict(arguments or {})
    prop_name = ((focus.get("property") or {}).get("name") or "").strip()
    if not prop_name:
        return args

    def is_referent(value) -> bool:
        return str(value or "").strip().casefold() in _REFERENT_WORDS

    if "property_query" in args and is_referent(args.get("property_query")):
        args["property_query"] = prop_name
    if (
        tool_name in {"update", "read", "link"}
        and str(args.get("entity") or "").strip().casefold() == "property"
        and is_referent(args.get("query"))
    ):
        args["query"] = prop_name
    return args


# An attached photo arrives with routing guidance appended to the landlord's
# message (comms/tasks.py, views.py). That guidance necessarily NAMES the words
# we scan for — "document, mail, letter, receipt, invoice, notice, statement, or
# paperwork" — so scanning the raw stored text made every attachment look like a
# business record, whatever the landlord actually said. Everything from the
# marker onwards is ours, not theirs.
_ATTACHMENT_MARKER = re.compile(r"\[The landlord attached a photo", re.IGNORECASE)


def _landlord_words(text: str) -> str:
    """The part of a stored message the landlord actually typed."""
    return _ATTACHMENT_MARKER.split(text or "", maxsplit=1)[0]


# "these are NOT business documents" / "it isn't a receipt". Without this the
# keyword scan below matched the very noun the landlord was ruling out.
_DENIES_BUSINESS_RECORD = re.compile(
    r"\b(not|isn'?t|aren'?t|no)\b[^.!?]{0,40}?\b"
    r"(business|document|documents|receipt|receipts|invoice|invoices|"
    r"statement|statements|mail|letter|paperwork|notice)\b",
    re.IGNORECASE,
)

# The positive form: the landlord saying where the photos should actually go.
# Without an explicit claim, bare photo attachments default to the document/OCR
# path — weak models otherwise invent "inspection photo" for clear receipts.
_CLAIMS_LISTING_PHOTO = re.compile(
    r"\b("
    r"gallery|listing photo|listing photos|listing image|listing images|"
    r"property photo|property photos|main photo|primary photo|cover photo|"
    r"marketing photo|marketing photos|inspection photo|inspection photos|"
    r"for the listing|to the listing|on the listing|"
    r"add (?:this |these )?(?:to|on) (?:the )?(?:listing|room|suite|unit)|"
    r"just (?:some )?(?:images|photos|pics|pictures)"
    r")\b",
    re.IGNORECASE,
)


def _conversation_attachment_focus(landlord, conversation_id) -> dict:
    """Keep an unresolved attachment visible after its original upload turn."""
    rows = RamaAudit.objects.filter(
        landlord=landlord,
        conversation_id=conversation_id,
        kind=RamaAudit.Kind.USER_MESSAGE,
    ).order_by("-created_at")[:12]
    raw_texts = [str((row.content or {}).get("text") or "") for row in rows]
    # IDs are extracted from explicit message markers only. Never scan the
    # landlord's global unused-upload pool: that was the 11-files-became-28 bug.
    joined_raw = "\n".join(raw_texts)
    document_ids = set(
        re.findall(r"Business document ([0-9a-fA-F-]{32,36})", joined_raw),
    )
    legacy_upload_ids = set(
        re.findall(r"upload_id=([0-9a-fA-F-]{32,36})", joined_raw),
    )
    batch_ids = re.findall(
        r"RAMA attachment batch ([0-9a-fA-F-]{32,36})",
        joined_raw,
    )
    texts = [_landlord_words(t) for t in raw_texts]
    combined = "\n".join(texts)
    from .models import RamaAttachment
    from .models import RamaAttachmentBatch
    from .models import RamaUpload

    upload_ids = {
        str(pk)
        for pk in RamaUpload.objects.filter(
            landlord=landlord,
            used_at__isnull=True,
            pk__in=legacy_upload_ids,
        ).values_list("pk", flat=True)
    }
    attachment_batch = None
    attachment_ids: list[str] = []
    # raw_texts are newest first and each marker is unique; use the newest
    # still-actionable batch only. A correction never silently merges batches.
    for batch_id in batch_ids:
        candidate = RamaAttachmentBatch.objects.filter(
            pk=batch_id,
            landlord=landlord,
            conversation_id=conversation_id,
        ).first()
        if candidate is None:
            continue
        pending_ids = list(
            candidate.attachments.filter(
                status__in=[
                    RamaAttachment.Status.STAGED,
                    RamaAttachment.Status.CLASSIFIED,
                ],
            )
            .order_by("sequence")
            .values_list("pk", flat=True),
        )
        if pending_ids:
            attachment_batch = candidate
            attachment_ids = [str(pk) for pk in pending_ids]
            break

    if document_ids:
        from .models import RamaDocument

        document_ids = {
            str(pk)
            for pk in RamaDocument.objects.filter(
                landlord=landlord,
                pk__in=document_ids,
            ).values_list("pk", flat=True)
        }
    if not upload_ids and not document_ids and not attachment_ids:
        return {}
    lowered = combined.casefold()
    business_terms = (
        "document",
        "mail",
        "letter",
        "receipt",
        "invoice",
        "invoices",
        "notice",
        "statement",
        "paperwork",
        "scotiabank",
        "bank",
        "ocr",
        "scan",
        "scanned",
        "pdf",
        "expense",
        "maintenance expense",
        "bill",
        "bills",
        "payable",
        "installing",
        "installation",
        "contractor",
        "vendor",
        "tax notice",
        "property tax",
    )
    business_record = any(term in lowered for term in business_terms)

    # File shape also forces the document path — a PDF receipt is not gallery media.
    if attachment_batch is not None:
        for row in attachment_batch.attachments.filter(pk__in=attachment_ids):
            name = str(row.original_filename or "").casefold()
            ctype = str(row.content_type or "").casefold()
            if (
                ctype.startswith("application/pdf")
                or name.endswith((".pdf", ".tif", ".tiff"))
                or any(
                    token in name
                    for token in ("receipt", "invoice", "statement", "notice", "bill")
                )
            ):
                business_record = True
                break
    # A bare substring test read "these are NOT business documents" as a
    # business document, because it matched "document" and never looked at the
    # word in front of it. Correcting RAMA therefore reinforced the mistake it
    # was being corrected for. Both overrides below are checked against the
    # LATEST message only — the landlord's most recent words win.
    latest = (texts[0] if texts else "").casefold()
    claims_listing = bool(_CLAIMS_LISTING_PHOTO.search(latest))
    denies_business = bool(_DENIES_BUSINESS_RECORD.search(latest))
    if denies_business or claims_listing:
        business_record = False
    elif not business_record:
        # DEFAULT: bare photo/file with no listing intent → document/OCR path.
        # Landlords drop clear receipts with no caption; models then invent
        # "property/inspection photo". Listing media almost always comes with
        # "gallery / listing / for Room X / main photo". OCR first is safe —
        # it never posts money and never attaches to a listing.
        business_record = True
    issuer = "Scotiabank" if "scotiabank" in lowered else ""
    document_date = ""
    for pattern, fmt in (
        (r"\b([A-Z][a-z]+ \d{1,2} \d{4})\b", "%B %d %Y"),
        (r"\b(\d{1,2} [A-Z][a-z]+ \d{4})\b", "%d %B %Y"),
    ):
        match = re.search(pattern, combined)
        if not match:
            continue
        try:
            document_date = datetime.strptime(match.group(1), fmt).date().isoformat()
            break
        except ValueError:
            continue
    pending_count = len(upload_ids) + len(attachment_ids)
    return {
        "unresolved_upload_ids": sorted(upload_ids),
        "attachment_batch_id": (
            str(attachment_batch.pk) if attachment_batch is not None else None
        ),
        "attachment_ids": attachment_ids,
        "document_ids": sorted(document_ids),
        "landlord_described_as_business_record": business_record,
        "landlord_claims_listing_photo": claims_listing,
        # Stated explicitly because the model was guessing at how many photos
        # it had and consistently guessing low.
        "pending_photo_count": pending_count,
        "issuer": issuer or None,
        "document_date": document_date or None,
        "instruction": (
            f"The landlord has {pending_count} attached file(s) not yet placed "
            f"(attachment_batch_id={attachment_batch.pk if attachment_batch else None}; "
            f"attachment_ids={attachment_ids or sorted(upload_ids)}). "
            + (
                "DEFAULT = business document. Call catalog_business_document "
                "with attachment_id/upload_id ONLY first (no scope_query) — "
                "hash + OCR. Do NOT call attach_photo_to_listing. Do NOT say "
                "this 'looks like a property/inspection photo'. Do NOT ask for "
                "the address before that OCR call. Only if needs_input, ask the "
                "holding address, then catalog with document_id + scope_query. "
                "NEVER invent amounts. NEVER re-file duplicates. NEVER claim "
                "you lack OCR."
                if business_record
                else (
                    "Landlord indicated LISTING/property media. Call "
                    "attach_photo_to_listing ONCE with attachment_batch_id from "
                    "this focus (or upload_id for legacy). Never substitute an "
                    "older batch. If they meant a receipt after all, use "
                    "catalog_business_document (no address first) instead."
                )
            )
        ),
    }


# Verbal expenses without a receipt photo this turn.
_VERBAL_EXPENSE_RE = re.compile(
    r"\b(bought|purchased|spent|i paid|paid \$?\d|cost me|expense for)\b",
    re.IGNORECASE,
)
_NO_RECEIPT_RE = re.compile(
    r"(didn'?t send|no (receipt|picture|photo)|lost (it|the receipt)|"
    r"without (a )?(receipt|photo|picture)|just (log|file|record|post) (the )?expense|"
    r"don'?t (need to )?catalog|not a (receipt|document)|something new|"
    r"already been recorded|already recorded)",
    re.IGNORECASE,
)
# Follow-up on a photographed receipt — never create_expense with the chat text.
_RECEIPT_FOLLOWUP_RE = re.compile(
    r"\b(receipt|document|invoice|pdf|photo of (the )?(receipt|bill)|"
    r"store (this|it) as|file (this|it) as|just store|attach (this|it)|"
    r"link (this|it|to)|expense is (already )?(logged|recorded|posted)|"
    r"(already|is) (logged|recorded|posted)|gift card|"
    r"no it'?s \$?\d|the \$?[\d.]+ (figure|amount|was|is)|"
    r"\$?[\d.]+ (figure )?was just|not (the )?\$?[\d.]+\b|"
    r"u should know|you should know)\b",
    re.IGNORECASE,
)
_AMOUNT_CORRECTION_RE = re.compile(
    r"(?:no[, ]+(?:it'?s|its|the (?:real |actual )?amount is|amount is)|"
    r"(?:actually|correct(?:ly)?|real|actual) (?:amount |total )?|"
    r"(?:amount|total) (?:is|should be|was) )\s*\$?\s*(\d+(?:\.\d{1,2})?)"
    r"|(?:^|[^\d])\$\s*(\d+\.\d{2})\b",
    re.IGNORECASE,
)
# "void the expense" must NEVER become create_expense with description "void…".
_VOID_EXPENSE_RE = re.compile(
    r"\b(void|reverse|undo|cancel|delete|remove)\b.{0,40}\b("
    r"expense|charge|entry|ledger|posting|cost|bill)\b"
    r"|\bvoid\b.{0,20}\b(the |this |that |wrong |both |two )?"
    r"|\b(void|reverse) (it|them|both|those)\b",
    re.IGNORECASE,
)


def _message_has_new_file(message: str) -> bool:
    text = message or ""
    return bool(
        re.search(r"upload_id=", text)
        or re.search(r"RAMA attachment batch", text)
        or re.search(r"Business document [0-9a-fA-F-]{32,36}", text)
    )


def _void_expense_intent(landlord, message: str) -> dict | None:
    """Parse 'void the $125 window screens expense' → void_ledger_entry."""
    if _message_has_new_file(message):
        return None
    text = _landlord_words(message or "").strip()
    if not text or not _VOID_EXPENSE_RE.search(text):
        return None
    money = re.search(r"\$\s*(\d+(?:\.\d{1,2})?)", text)
    if not money:
        money = re.search(r"\b(\d+\.\d{2})\b", text)
    amount = money.group(1) if money else ""
    # Prefer distinctive words from the message for description_query.
    # Strip void-command noise so we match the ORIGINAL expense text.
    q = text
    q = re.sub(
        r"\b(void|reverse|undo|cancel|delete|remove|the wrong|wrong|both|two|"
        r"these|those|please|expense|charge|entry|ledger|not yet taken|"
        r"already paid|paid)\b",
        " ",
        q,
        flags=re.IGNORECASE,
    )
    q = re.sub(r"\$\s*\d+(?:\.\d{1,2})?", " ", q)
    q = re.sub(r"[−–—-]\s*\$?", " ", q)
    q = re.sub(r"\s+", " ", q).strip(" .,;:\"'")
    # Fallback keywords for common cases.
    if len(q) < 4 and "screen" in text.casefold():
        q = "window screens"
    if len(q) < 4 and amount:
        q = amount
    if not q and not amount:
        return None
    reason = "Landlord requested void via chat"
    if "wrong" in text.casefold():
        reason = "Posted in error / wrong expense"
    # void_all when they say both/two/all of these $X
    void_all = bool(
        re.search(r"\b(both|two|all|every|each)\b", text, re.IGNORECASE)
    )
    return {
        "tool": "void_ledger_entry",
        "arguments": {
            "description_query": q[:120] if q else "",
            "amount": amount,
            "reason": reason,
            "void_all": "yes" if void_all else "",
        },
    }


def _looks_like_receipt_followup(message: str) -> bool:
    """True when the landlord is correcting/filing a receipt, not a bare cash log."""
    text = _landlord_words(message or "")
    if not text:
        return False
    if _RECEIPT_FOLLOWUP_RE.search(text):
        return True
    # "No it's $13.41" / amount corrections without bought/purchased.
    if _AMOUNT_CORRECTION_RE.search(text) and not _VERBAL_EXPENSE_RE.search(text):
        return True
    return False


def _amount_from_message(message: str) -> str:
    """Prefer an explicit correction amount; else first money token."""
    text = _landlord_words(message or "")
    m = _AMOUNT_CORRECTION_RE.search(text)
    if m:
        return (m.group(1) or m.group(2) or "").strip()
    money = re.search(r"\$\s*(\d+(?:\.\d{1,2})?)", text)
    if money:
        return money.group(1)
    money = re.search(r"\b(\d+\.\d{2})\b", text)
    return money.group(1) if money else ""


def _wants_link_existing_expense(message: str) -> bool:
    text = (message or "").casefold()
    return bool(
        re.search(
            r"\b(expense is (already )?(logged|recorded|posted)|"
            r"(already|is) (logged|recorded|posted)|"
            r"just store (this |it )?(as )?(the )?(receipt|document)|"
            r"store this as (the )?(receipt|document)|"
            r"attach (to|as)|link (to|the )?(existing |logged )?(expense|entry)|"
            r"don'?t (create|post|log) (a )?(new )?expense|"
            r"receipt only|document only)\b",
            text,
        )
    )


def _wants_new_expense_not_link(message: str) -> bool:
    """Landlord rejects linking to an existing expense — file this as a new one."""
    text = (message or "").casefold()
    return bool(
        re.search(
            r"\b(new expense|separate expense|different expense|"
            r"not (the )?(same|existing|old) (one|expense)|"
            r"not (that|this) (expense|one)|"
            r"it'?s a new (one|expense|cost|purchase)|"
            r"no it'?s a new|"
            r"post (it |this )?(as )?(a )?new|"
            r"don'?t (link|attach)|not (a )?link)\b",
            text,
        )
    )


def _receipt_title_from_caption(caption: str, amount: str = "") -> str:
    """Short expense title from landlord words (drop amount/address noise)."""
    text = _landlord_words(caption or "")
    text = re.sub(r"\$\s*\d+(?:\.\d{1,2})?", " ", text)
    text = re.sub(r"\b\d+\.\d{2}\b", " ", text)
    text = re.sub(
        r"\b(no it'?s not a return|not a return|returns?|refunds?|"
        r"which physical|portfolio|included|expense for|bought|"
        r"purchased|today|please|the|and|for a|for an|new)\b",
        " ",
        text,
        flags=re.I,
    )
    text = re.sub(r"\s+", " ", text).strip(" .,;:-")
    if len(text) < 3:
        return f"Expense ${amount}" if amount else "Receipt expense"
    # Prefer a concise noun phrase.
    return text[:120].strip()


def _pending_unscoped_document_id(landlord, conversation_id) -> str:
    """Newest prepared document from this chat that still needs holding/filing."""
    from .models import RamaDocument

    # Prefer document_ids returned by recent tool calls in this conversation.
    rows = RamaAudit.objects.filter(
        landlord=landlord,
        conversation_id=conversation_id,
        kind=RamaAudit.Kind.TOOL_CALL,
    ).order_by("-created_at")[:30]
    seen: list[str] = []
    for row in rows:
        content = row.content or {}
        result = content.get("result") or {}
        args = content.get("arguments") or {}
        tool = str(content.get("tool") or "")
        if tool and "document" not in tool and "catalog" not in tool:
            continue
        for candidate in (
            result.get("document_id") if isinstance(result, dict) else None,
            args.get("document_id") if isinstance(args, dict) else None,
        ):
            if candidate and str(candidate) not in seen:
                seen.append(str(candidate))
    if not seen:
        # Fallback: newest unscoped doc for landlord (same as catalog tool bare path).
        pending = (
            RamaDocument.objects.filter(
                landlord=landlord, holding__isnull=True, deleted_at__isnull=True
            )
            .exclude(status=RamaDocument.Status.FILED)
            .order_by("-created_at")
            .first()
        )
        return str(pending.pk) if pending else ""

    for doc_id in seen:
        doc = (
            RamaDocument.objects.filter(
                pk=doc_id, landlord=landlord, deleted_at__isnull=True
            )
            .exclude(status=RamaDocument.Status.FILED)
            .first()
        )
        if doc is None:
            continue
        # Unscoped, or scoped but not yet linked/filed — still "pending".
        if not doc.holding_id or not doc.ledger_entry_id:
            return str(doc.pk)
    return ""


def _focus_has_pending_file(focus: dict | None) -> bool:
    focus = focus or {}
    return bool(
        focus.get("unresolved_upload_ids")
        or focus.get("attachment_ids")
        or focus.get("document_ids")
    )


def _apply_document_amount_correction(landlord, document_id: str, amount: str) -> None:
    if not document_id or not amount:
        return
    from decimal import Decimal, InvalidOperation

    from .models import RamaDocument

    try:
        value = Decimal(str(amount))
    except (InvalidOperation, TypeError, ValueError):
        return
    if value <= 0:
        return
    doc = RamaDocument.objects.filter(pk=document_id, landlord=landlord).first()
    if doc is None or doc.status == RamaDocument.Status.FILED:
        return
    if doc.amount == value:
        return
    old = doc.amount
    doc.amount = value
    # Landlord overrode OCR (e.g. gift-card line misread as total).
    data = dict(doc.extracted_data or {})
    data["amount_source"] = "landlord_correction"
    data["amount_ocr_was"] = str(old) if old is not None else None
    doc.extracted_data = data
    doc.save(update_fields=["amount", "extracted_data", "updated_at"])


def _verbal_expense_intent(landlord, message: str, live_portfolio: dict) -> dict | None:
    """Parse 'I bought X for $Y at McKenzie, paid' with no photo → create_expense.

    Must not steal turns that still have a fresh attachment, and must not re-open
    an earlier OCR receipt when the landlord is stating a new cash expense.
    """
    if _message_has_new_file(message):
        return None
    text = _landlord_words(message or "").strip()
    if not text:
        return None
    # Void/reverse is a ledger control — never create_expense.
    if _VOID_EXPENSE_RE.search(text) or text.casefold().startswith("void "):
        return None
    # Receipt correction / "store as document" is never a verbal create.
    if _looks_like_receipt_followup(text):
        return None
    # Explicit "no receipt / something new" is enough even without "bought".
    looks_expense = bool(_VERBAL_EXPENSE_RE.search(text)) or bool(
        re.search(r"\$\s*\d", text) and re.search(r"\b(for|at|on)\b", text)
    )
    if not looks_expense and not _NO_RECEIPT_RE.search(text):
        return None
    # Bare "$X for address" without bought/purchased is too often a receipt
    # correction — require explicit buy language or no-receipt language.
    if not _VERBAL_EXPENSE_RE.search(text) and not _NO_RECEIPT_RE.search(text):
        return None
    money = re.search(r"\$\s*(\d+(?:\.\d{1,2})?)", text)
    if not money:
        money = re.search(r"\b(\d+\.\d{2})\b", text)
    if not money:
        return None
    amount = money.group(1)
    holding = _address_scope_from_message(text, live_portfolio)
    desc = text
    desc = re.sub(r"\$\s*\d+(?:\.\d{1,2})?", "", desc)
    desc = re.sub(r"\b\d+\.\d{2}\b", "", desc)
    desc = re.sub(
        r"\b(i bought|bought|purchased|spent|i paid|paid today|already paid|"
        r"its paid|it's paid|and it'?s paid|today)\b",
        "",
        desc,
        flags=re.IGNORECASE,
    )
    desc = re.sub(r"\s+", " ", desc).strip(" .,;:")
    if len(desc) < 2:
        desc = f"Expense ${amount}"
    paid = bool(
        re.search(
            r"\b(paid|already paid|its paid|it's paid|and it'?s paid)\b",
            text,
            re.IGNORECASE,
        )
    )
    return {
        "tool": "create_expense",
        "arguments": {
            "amount": amount,
            "description": desc[:200],
            "holding_name": holding or "",
            "property_query": "",
            "paid_on": "today" if paid else "",
            "effective_date": "",
            "category": "",
        },
    }


def _address_scope_from_message(message: str, live_portfolio: dict) -> str:
    """Return one known legal address mentioned in the current message.

    Accepts exact normalised containment ("950 mckenzie ave" inside the note)
    and distinctive street tokens ("mckenzie") when only one portfolio address
    matches — so a receipt for "950 McKenzie ave house" scopes without forcing
    a room listing.
    """
    from .document_services import _normalise_address

    needle = _normalise_address(message or "")
    if not needle:
        return ""
    candidates: list[str] = []
    seen: set[str] = set()
    for row in (live_portfolio.get("listings") or []):
        address = str(row.get("address") or "").strip()
        if not address:
            continue
        key = _normalise_address(address)
        if not key or key in seen:
            continue
        seen.add(key)
        if key in needle or all(
            token in needle for token in key.split() if len(token) > 2
        ):
            candidates.append(address)
            continue
        # Distinctive street name only (e.g. "mckenzie") when unique in portfolio.
        tokens = [t for t in key.split() if t.isalpha() and len(t) >= 5]
        if any(token in needle for token in tokens):
            candidates.append(address)
    # De-dupe while preserving order.
    ordered: list[str] = []
    seen_addr: set[str] = set()
    for address in candidates:
        k = _normalise_address(address)
        if k in seen_addr:
            continue
        seen_addr.add(k)
        ordered.append(address)
    if len(ordered) == 1:
        return ordered[0]
    # Prefer the longest exact containment match when several share a street.
    exact = [
        address
        for address in ordered
        if _normalise_address(address) in needle
    ]
    if len(exact) == 1:
        return exact[0]
    return ""


def _document_preview_reply(result: dict) -> str:
    preview = result.get("preview") or {}
    children = preview.get("child_listings") or []
    parts = [
        "Ready to store this as a business document for the physical property:",
        f"• Address: {preview.get('scope')}",
        "• Individual listing: none",
    ]
    if children:
        parts.append("• Child listings under it: " + ", ".join(children))
    if preview.get("issuer"):
        parts.append(f"• Issuer: {preview['issuer']}")
    if preview.get("document_date"):
        parts.append(f"• Document date: {preview['document_date']}")
    if preview.get("convert_photo_to_ocr_document"):
        parts.append("• Storage: OCR + archival PDF")
    if preview.get("create_holding"):
        parts.append("• A physical holding will be created for this exact address")
    parts.append("Reply yes to apply this filing, or no to cancel.")
    return "\n".join(parts)


def _document_location_request(message: str) -> bool:
    text = (message or "").casefold()
    terms = ("directory", "folder", "location", "path", "manually", "manual")
    return any(term in text for term in terms) or (
        "where" in text and any(term in text for term in ("document", "file", "stored"))
    )


def _recent_document_id(landlord, conversation_id, message: str) -> str:
    explicit = re.search(
        r"\b[0-9a-fA-F]{8}-[0-9a-fA-F-]{27,28}\b", message or "",
    )
    if explicit:
        return explicit.group(0)
    rows = RamaAudit.objects.filter(
        landlord=landlord,
        conversation_id=conversation_id,
        kind=RamaAudit.Kind.TOOL_CALL,
    ).order_by("-created_at")[:20]
    for row in rows:
        content = row.content or {}
        args = content.get("arguments") or {}
        result = content.get("result") or {}
        for candidate in (
            result.get("document_id") if isinstance(result, dict) else None,
            args.get("document_id") if isinstance(args, dict) else None,
        ):
            if candidate:
                return str(candidate)
    return ""


def _document_location_reply(result: dict) -> str:
    parts = [
        f"Document: {result.get('title')}",
        f"Storage key: {result.get('storage_key')}",
        f"Manual location: {result.get('manual_location')}",
    ]
    if result.get("container_path"):
        parts.append(f"Container path: {result['container_path']}")
    else:
        parts.append(
            "Container path: none — production stores this file in object storage.",
        )
    parts.extend(
        [
            f"Documents page: {result.get('documents_page')}",
            "Authenticated download endpoint: "
            f"{result.get('authenticated_download_path')}",
        ],
    )
    return "\n".join(parts)


def _delegate(landlord, tool_name: str, arguments: dict) -> dict:
    """Run a bounded sub-turn for a delegation call from the General.

    The sub-agent's pending plan (if any) is re-homed onto the caller by
    returning it as a needs_confirm payload — the outer turn persists it on
    the GENERAL's conversation, so the landlord's next "yes" runs it through
    the same deterministic confirm machine.
    """
    sub_role = {"ask_fsa": "fsa", "ask_treasurer": "treasurer"}.get(
        tool_name, "corporal",
    )
    instruction = str(
        (arguments or {}).get("instruction")
        or (arguments or {}).get("question")
        or "",
    ).strip()
    if not instruction:
        return {"error": "instruction is required."}

    sub_cid = uuid.uuid4()
    sub = run_turn(
        landlord, instruction, sub_cid, role=sub_role, channel="system", depth=1,
    )
    if sub.error is not None:
        return {"error": f"{sub_role} unavailable: {sub.error['detail']}"}

    out: dict = {"role": sub_role, "answer": sub.reply, "tools_used": sub.tools_used}
    if sub.auto_executed:
        # A sub-turn that auto-ran leaves no plan to re-home, so the delegating
        # turn has to carry the receipts out itself or the landlord would never
        # be told (or offered the undo).
        out["auto_executed"] = sub.auto_executed
        out["instruction"] = (
            f"The {sub_role} already ran these — they were pre-authorised in "
            "the landlord's Constitution. Report them as done, not as a "
            "proposal, and mention they can be undone."
        )
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
    # Actions this turn ran without asking, under the landlord's Constitution
    # autonomy rule. Each is {"id", "tool", "target", "undoable"} — the UI
    # renders them as a "Done automatically · Undo" strip.
    auto_executed: list[dict] = field(default_factory=list)
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
    focus = _conversation_focus(
        landlord, conversation_id, message, safe_context,
    )
    if focus:
        system += (
            "\n\n## CONVERSATION FOCUS (deterministic follow-up resolution)\n"
            + json.dumps(focus, separators=(",", ":"))
        )
    attachment_focus = _conversation_attachment_focus(landlord, conversation_id)
    if attachment_focus:
        system += (
            "\n\n## ACTIVE ATTACHMENT FOCUS (persists across follow-ups)\n"
            + json.dumps(attachment_focus, separators=(",", ":"))
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
    # The only part of the prompt that survives between conversations. Bounded,
    # deterministic, and explicitly subordinate to the live portfolio card —
    # see rama/memory.py for why it may never hold portfolio state.
    memory_block = memory.render_for_prompt(landlord, message, focus)
    if memory_block:
        system += "\n\n" + memory_block
        audit(
            RamaAudit.Kind.TOOL_CALL,
            {
                "tool": "_memory",
                "arguments": {},
                "result": {"injected": memory_block.count("\n- ") or 0},
            },
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
    if getattr(settings, "RAMA_COMMAND_ENGINE_V2", True):
        schemas = select_tool_schemas(
            message,
            schemas,
            limit=max(6, int(getattr(settings, "RAMA_TOOL_RETRIEVAL_LIMIT", 12))),
        )
    max_rounds = SUB_TURN_MAX_ROUNDS if depth >= 1 else MAX_TOOL_ROUNDS
    tools_used: list[str] = ["_live_context"]
    # Tools whose RESULT said a change landed. Distinct from tools_used: a
    # model-issued write call has its confirm blanked, so it previews and
    # writes nothing — see _is_write_result.
    turn_writes: list[str] = []
    # Warnings carried by previews this turn, appended to the reply verbatim.
    preview_warnings: list[str] = []
    # Receipts for anything this turn ran unattended — populated by the
    # deterministic memory router and by delegated sub-turns, both of which can
    # fire before the main tool loop's own autonomy check.
    delegated_auto: list[dict] = []
    turn_attachments: list[dict] = []
    turn = Turn()

    def _run_deterministic_tool(tool_name: str, arguments: dict) -> dict:
        # The routers below call this directly, so they never pass through
        # role_tool_schemas(). Without this check a role's tool list describes
        # only what the MODEL may ask for — a read-only role could still reach
        # a write tool by phrasing a message that matches a router's regex.
        # Checking here covers every existing router and every future one.
        if not role_allows_tool(role, tool_name):
            return {
                "error": (
                    f"The {role} agent is not permitted to call {tool_name}."
                ),
            }
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
                    },
                )
            elif result.get("needs_input"):
                questions.append(str(result.get("question_for_user") or ""))
            elif result.get("unchanged") or result.get("idempotent"):
                excluded.append(
                    {
                        "target": arguments["name"],
                        "error": str(
                            result.get("message") or "No change is needed.",
                        ),
                    },
                )
            else:
                excluded.append(
                    {
                        "target": arguments["name"],
                        "error": str(result.get("error") or result),
                    },
                )
        if specs:
            plan = save_batch(landlord, conversation_id, specs)
            return _batch_preview_reply(plan, excluded)
        clear_plan(landlord, conversation_id)
        if questions:
            return "\n".join(
                dict.fromkeys(question for question in questions if question),
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
    # Date/time corrections while a viewing is awaiting Yes replace the plan
    # before affirmation can run the stale preview ("tomorrow" → "july 31").
    if pending_plan is not None and not _is_affirmative(message):
        amended = _amend_pending_schedule_from_message(
            landlord, conversation_id, pending_plan, message,
        )
        if amended is not None:
            deterministic_reply = amended
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
    elif pending_plan is not None and _is_affirmative(message) and deterministic_reply is None:

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
        summary = pending_plan.summary or ""
        was_link_preview = "existing expense" in summary.casefold() or (
            pending_plan.steps.filter(
                tool="file_business_document"
            ).exists()
            if hasattr(pending_plan, "steps")
            else False
        )
        clear_plan(landlord, conversation_id)
        audit(
            RamaAudit.Kind.TOOL_CALL,
            {"tool": "_plan_cancelled", "arguments": {}, "result": {"cancelled": summary}},
        )
        pending_plan = None
        # "No, it's a new expense" after a wrong link-preview: clear the link
        # plan and let the receipt/new-expense router file this document as a
        # fresh cost (do not stop with a dead-end "Cancelled" message).
        if _wants_new_expense_not_link(message) or (
            was_link_preview and re.search(r"\bnew\b", message or "", re.I)
        ):
            pass  # deterministic_reply stays None → document router below
        else:
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

    # "Remember that …" / "forget that …" — routed deterministically so a weak
    # model can't drop a preference the landlord explicitly asked it to keep.
    if deterministic_reply is None and pending_plan is None:
        intent = _memory_intent(message)
        if intent is not None:
            preview = _run_deterministic_tool(intent["tool"], intent["arguments"])
            if preview.get("error"):
                deterministic_reply = str(preview["error"])
            elif preview.get("needs_confirm"):
                spec = {
                    "kind": "single",
                    "tool": intent["tool"],
                    "arguments": intent["arguments"],
                    "target": intent["arguments"].get("subject", ""),
                }
                auto = autonomy_policy.evaluate_turn(
                    landlord,
                    [spec],
                    role=role,
                    channel=channel,
                    had_pending_plan=False,
                )
                if auto.approved:
                    plan = save_batch(landlord, conversation_id, [spec])

                    def _mem_audit(content):
                        tools_used.append(content.get("tool", ""))
                        audit(
                            RamaAudit.Kind.TOOL_CALL,
                            {**content, "autonomous": True, "deterministic_routing": True},
                        )

                    progress = run_plan(plan, landlord, audit=_mem_audit)
                    receipts = autonomy_policy.record_auto_actions(
                        landlord, conversation_id, progress, auto.policy,
                    )
                    delegated_auto.extend(receipts)
                    deterministic_reply = (
                        _plan_fallback_reply(progress) + _undo_hint(receipts)
                    )
                else:
                    save_single(
                        landlord, conversation_id, intent["tool"], intent["arguments"],
                    )
                    deterministic_reply = _preview_reply(intent["tool"], preview)

    # "Undo" — reverse the last thing RAMA did on its own. Handled here, with
    # no provider round-trip, for the same reason yes/no is: the landlord
    # taking something back must not depend on the model picking a tool.
    if (
        deterministic_reply is None
        and pending_plan is None
        and _is_undo_request(message)
    ):
        action = autonomy_policy.undoable_actions(landlord).first()
        if action is None:
            deterministic_reply = (
                "There's nothing of mine to undo — I haven't run anything "
                "automatically in the last day."
            )
        else:

            def _undo_audit(content):
                tools_used.append(content.get("tool", ""))
                audit(RamaAudit.Kind.TOOL_CALL, {**content, "undo_of": str(action.pk)})

            outcome = autonomy_policy.undo_action(action, landlord, audit=_undo_audit)
            deterministic_reply = (
                str(outcome["error"])
                if outcome.get("error")
                else "Undone — "
                + _plan_fallback_reply(outcome["progress"]).strip().rstrip(".")
                + "."
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
                    or result,
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
                    or result,
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
                intent["tool"], intent["arguments"],
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
                    result.get("error") or result.get("message") or result,
                )

    # Practical money/lease/viewing status chains (backend can do these — always
    # prefer tools over "I can't" / capability-gap hallucinations).
    if deterministic_reply is None and pending_plan is None:
        low_msg = (message or "").casefold()
        # "has Siya signed/seen the lease?"
        if re.search(
            r"\b(has|have|did)\b.+\b(signed|seen|viewed|opened)\b.+\b(lease|invite)\b"
            r"|\b(signed|seen)\b.+\b(lease|invite)\b"
            r"|\bhas \w+ signed\b|\bhas \w+ seen\b",
            low_msg,
        ) and not re.search(r"\b(viewing|showing)\b", low_msg):
            person = ""
            # Prefer explicit capitalized name: "has Siya signed"
            m2 = re.search(r"\bhas\s+([A-Z][a-zA-Z'’\-]+)\b", message or "")
            if m2 and m2.group(1).casefold() not in {
                "the", "she", "he", "they", "anyone", "someone",
            }:
                person = m2.group(1)
            if not person:
                m3 = re.search(
                    r"\b([A-Z][a-zA-Z'’\-]+)\s+(signed|seen|viewed|opened)\b",
                    message or "",
                )
                if m3:
                    person = m3.group(1)
            if not person:
                m = re.search(
                    r"\bhas\s+([A-Za-z][A-Za-z'’\-]+)\b",
                    message or "",
                    re.I,
                )
                if m and m.group(1).casefold() not in {
                    "the", "she", "he", "they", "anyone", "someone",
                }:
                    person = m.group(1)
            result = execute(
                "tenant_lease_status",
                {"person_query": person or (message or "")[:80]},
                landlord=landlord,
            )
            tools_used.append("tenant_lease_status")
            audit(
                RamaAudit.Kind.TOOL_CALL,
                {
                    "tool": "tenant_lease_status",
                    "arguments": {"person_query": person},
                    "result": json.loads(json.dumps(result, default=str)),
                    "deterministic_routing": True,
                },
            )
            deterministic_reply = str(
                result.get("message") or result.get("error") or result
            )

    if deterministic_reply is None and pending_plan is None:
        low_msg = (message or "").casefold()
        # Mark expense paid / "not yet taken" follow-ups
        if re.search(
            r"\b(mark|marked).+\bpaid\b"
            r"|\bneeds? to be (marked )?paid\b"
            r"|\bnot yet taken\b"
            r"|\bwhy does it say not yet\b"
            r"|\b(expense|draino|invoice).+\bpaid\b",
            low_msg,
        ) and not re.search(r"\b(cleaning fee)\b", low_msg):
            amt_m = re.search(r"\$?\s*(\d+\.\d{2})\b", message or "")
            desc = re.sub(
                r"\b(mark|marked|needs?|to be|as paid|paid|the|expense|"
                r"not yet taken|why does it say|please)\b",
                " ",
                message or "",
                flags=re.I,
            )
            desc = re.sub(r"\$?\s*\d+\.\d{2}", " ", desc)
            desc = re.sub(r"\s+", " ", desc).strip(" .,")
            args = {
                "description_query": desc[:80] if desc else "expense",
                "amount": amt_m.group(1) if amt_m else "",
                "paid_on": "today",
            }
            # Prefer mark_ledger_paid — if multi-match, tool returns candidates
            result = execute("mark_ledger_paid", args, landlord=landlord)
            tools_used.append("mark_ledger_paid")
            audit(
                RamaAudit.Kind.TOOL_CALL,
                {
                    "tool": "mark_ledger_paid",
                    "arguments": args,
                    "result": json.loads(json.dumps(result, default=str)),
                    "deterministic_routing": True,
                },
            )
            if result.get("needs_confirm"):
                save_single(landlord, conversation_id, "mark_ledger_paid", args)
                deterministic_reply = _preview_reply("mark_ledger_paid", result)
            else:
                deterministic_reply = str(
                    result.get("message") or result.get("error") or result
                )

    if deterministic_reply is None and pending_plan is None:
        low_msg = (message or "").casefold()
        # Reschedule existing viewing (not a brand-new schedule)
        if re.search(
            r"\b(reschedule|re-schedule|change|move)\b.+\b(viewing|showing|time|date)\b"
            r"|\b(viewing|showing)\b.+\b(reschedule|change|move)\b"
            r"|\bit should be on\b.+\b(am|pm|\d{1,2}:\d{2}|july|august|today|tomorrow)\b",
            low_msg,
        ) and not re.search(r"\b(make|create|book|schedule) a (new )?viewing\b", low_msg):
            # If there's already a pending schedule_viewing, amend path handles it.
            # Otherwise reschedule the latest matching scheduled viewing.
            when = _relative_when_from_text(message)
            contact = ""
            em = re.search(
                r"([A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,})",
                message or "",
            )
            if em:
                contact = em.group(1)
            else:
                # pull from recent schedule
                for row in RamaAudit.objects.filter(
                    landlord=landlord,
                    conversation_id=conversation_id,
                    kind=RamaAudit.Kind.TOOL_CALL,
                ).order_by("-created_at")[:15]:
                    content = row.content or {}
                    if content.get("tool") in {
                        "schedule_viewing",
                        "reschedule_viewing",
                    }:
                        args0 = content.get("arguments") or {}
                        contact = (
                            args0.get("contact_email")
                            or args0.get("contact_name")
                            or args0.get("contact")
                            or ""
                        )
                        if contact:
                            break
            prop = "Garden Suite" if "garden" in low_msg else ""
            args = {
                "when": when,
                "contact": contact,
                "property_query": prop,
                "notes": "",
            }
            result = execute("reschedule_viewing", args, landlord=landlord)
            tools_used.append("reschedule_viewing")
            audit(
                RamaAudit.Kind.TOOL_CALL,
                {
                    "tool": "reschedule_viewing",
                    "arguments": args,
                    "result": json.loads(json.dumps(result, default=str)),
                    "deterministic_routing": True,
                },
            )
            if result.get("needs_confirm"):
                save_single(
                    landlord, conversation_id, "reschedule_viewing", args,
                )
                deterministic_reply = _preview_reply(
                    "reschedule_viewing", result,
                )
            elif result.get("already_done"):
                deterministic_reply = str(
                    result.get("message")
                    or f"Already at {result.get('when') or when}."
                )
            else:
                deterministic_reply = str(
                    result.get("message") or result.get("error") or result
                )

    if deterministic_reply is None and pending_plan is None:
        low_msg = (message or "").casefold()
        if re.search(
            r"\bcancel\b.+\b(viewing|showing)\b"
            r"|\b(viewing|showing)\b.+\bcancel\b",
            low_msg,
        ):
            contact = ""
            em = re.search(
                r"([A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,})",
                message or "",
            )
            if em:
                contact = em.group(1)
            else:
                m = re.search(
                    r"\bfor\s+([A-Z][a-zA-Z'’-]+(?:\s+[A-Z][a-zA-Z'’-]+)?)",
                    message or "",
                )
                if m:
                    contact = m.group(1)
            args = {
                "contact": contact,
                "property_query": (
                    "Garden Suite" if "garden" in low_msg else ""
                ),
                "reason": "Landlord cancelled via chat",
            }
            result = execute("cancel_viewing", args, landlord=landlord)
            tools_used.append("cancel_viewing")
            audit(
                RamaAudit.Kind.TOOL_CALL,
                {
                    "tool": "cancel_viewing",
                    "arguments": args,
                    "result": json.loads(json.dumps(result, default=str)),
                    "deterministic_routing": True,
                },
            )
            if result.get("needs_confirm"):
                save_single(landlord, conversation_id, "cancel_viewing", args)
                deterministic_reply = _preview_reply("cancel_viewing", result)
            else:
                deterministic_reply = str(
                    result.get("message") or result.get("error") or result
                )

    # "Have they seen the viewing link?" — never claim we cannot track opens.
    if deterministic_reply is None and pending_plan is None:
        low_msg = (message or "").casefold()
        if re.search(
            r"\b(seen|opened|clicked|viewed)\b.+\b(viewing|invite|link|status)\b"
            r"|\b(viewing|invite)\b.+\b(seen|opened|clicked)\b"
            r"|\bhave they (seen|opened)\b",
            low_msg,
        ):
            # Prefer contact email/name from recent schedule or the message.
            contact = ""
            em = re.search(
                r"([A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,})",
                message or "",
            )
            if em:
                contact = em.group(1)
            else:
                nm = re.search(
                    r"\bfor\s+([A-Z][a-zA-Z'’-]+(?:\s+[A-Z][a-zA-Z'’-]+)?)",
                    message or "",
                )
                # Fallback: any capitalised name token after "has/have"
                if not nm:
                    nm = re.search(
                        r"\b(?:has|have)\s+([A-Z][a-zA-Z'’-]+)",
                        message or "",
                    )
                if nm:
                    contact = nm.group(1)
            # From recent audit if still blank
            if not contact:
                for row in RamaAudit.objects.filter(
                    landlord=landlord,
                    conversation_id=conversation_id,
                    kind=RamaAudit.Kind.TOOL_CALL,
                ).order_by("-created_at")[:20]:
                    content = row.content or {}
                    if content.get("tool") != "schedule_viewing":
                        continue
                    args = content.get("arguments") or {}
                    res = content.get("result") or {}
                    contact = (
                        args.get("contact_email")
                        or args.get("contact_name")
                        or (res.get("appointment") or {}).get("contact_email")
                        or (res.get("appointment") or {}).get("contact_name")
                        or ""
                    )
                    if contact:
                        break
            result = execute(
                "viewing_invite_status",
                {"contact": contact, "property_query": "", "appointment_ref": ""},
                landlord=landlord,
            )
            tools_used.append("viewing_invite_status")
            audit(
                RamaAudit.Kind.TOOL_CALL,
                {
                    "tool": "viewing_invite_status",
                    "arguments": {"contact": contact},
                    "result": json.loads(json.dumps(result, default=str)),
                    "deterministic_routing": True,
                },
            )
            if result.get("error") and result.get("matches"):
                lines = [str(result["error"])]
                for m in result["matches"][:6]:
                    lines.append(
                        f"• {m.get('contact_name')} {m.get('property')} "
                        f"{m.get('starts_at')} opened={m.get('opened')}"
                    )
                deterministic_reply = "\n".join(lines)
            else:
                deterministic_reply = str(
                    result.get("message") or result.get("error") or result
                )

    # Schedule viewing (with prospect email) BEFORE calendar nav link — "make a
    # viewing… send her an email" used to only return the Calendar URL.
    if deterministic_reply is None and pending_plan is None:
        intent = _schedule_viewing_intent(message)
        if intent is not None:
            result = execute(
                intent["tool"], intent["arguments"], landlord=landlord,
            )
            safe_result = json.loads(json.dumps(result, default=str))
            tools_used.append(intent["tool"])
            audit(
                RamaAudit.Kind.TOOL_CALL,
                {
                    "tool": intent["tool"],
                    "arguments": intent["arguments"],
                    "result": safe_result,
                    "deterministic_routing": True,
                },
            )
            if result.get("needs_confirm"):
                save_single(
                    landlord,
                    conversation_id,
                    intent["tool"],
                    intent["arguments"],
                )
                deterministic_reply = _preview_reply(intent["tool"], result)
            elif result.get("error"):
                deterministic_reply = str(result["error"])
            elif result.get("created"):
                deterministic_reply = _write_result_message(
                    intent["tool"], result,
                )

    if deterministic_reply is None and pending_plan is None:
        collection = _dashboard_collection_intent(message)
        if collection is not None:
            result = _run_deterministic_tool(
                "link", {"entity": collection, "query": ""},
            )
            deterministic_reply = str(
                result.get("error")
                or f"{result.get('label')}: {result.get('link')}",
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
        intent = _media_management_intent(landlord, conversation_id, message)
        if intent is not None:
            result = _run_deterministic_tool(
                intent["tool"],
                intent["arguments"],
            )
            attachment = result.get("_attachment")
            if isinstance(attachment, dict):
                turn_attachments.append(attachment)
            if result.get("needs_confirm"):
                save_single(
                    landlord,
                    conversation_id,
                    intent["tool"],
                    intent["arguments"],
                )
                deterministic_reply = _preview_reply(intent["tool"], result)
            elif result.get("error"):
                deterministic_reply = str(result["error"])
            elif intent["tool"] == "list_listing_media":
                deterministic_reply = _media_manifest_reply(result)
            else:
                deterministic_reply = str(result.get("note") or result)

    # A request to turn one existing suite into several named offerings must
    # keep every explicitly stated room and common area together. Sending this
    # through create_group_room loses the unit identity and forces a fragile
    # sequence of group writes; sending it to the model has historically
    # changed J/K into invented L/M. This router prepares one atomic preview
    # with the unit's stable UUID — or asks one focused question when the unit
    # or room names are still ambiguous. Never invent sequential letters.
    if deterministic_reply is None and pending_plan is None:
        intent = _unit_room_layout_intent(landlord, message)
        if intent is not None:
            if intent.get("deterministic_reply") and not intent.get("tool"):
                deterministic_reply = str(intent["deterministic_reply"])
            else:
                result = _run_deterministic_tool(
                    intent["tool"],
                    intent["arguments"],
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
                        result.get("error") or result.get("message") or result,
                    )

    if deterministic_reply is None and pending_plan is None:
        intent = _group_room_intent(landlord, message)
        if intent is not None:
            result = _run_deterministic_tool(
                intent["tool"], intent["arguments"],
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
                    result.get("error") or result.get("message") or result,
                )

    def _run_money_intent(intent: dict) -> str | None:
        result = execute(
            intent["tool"], intent["arguments"], landlord=landlord,
        )
        safe_result = json.loads(json.dumps(result, default=str))
        tools_used.append(intent["tool"])
        audit(
            RamaAudit.Kind.TOOL_CALL,
            {
                "tool": intent["tool"],
                "arguments": intent["arguments"],
                "result": safe_result,
                "deterministic_routing": True,
            },
        )
        if result.get("needs_confirm"):
            save_single(
                landlord,
                conversation_id,
                intent["tool"],
                intent["arguments"],
            )
            return _preview_reply(intent["tool"], result)
        if result.get("error"):
            # Multi-match for void → offer void_all path in plain language.
            if isinstance(result, dict) and result.get("matches"):
                lines = [str(result.get("error") or "Multiple matches:")]
                for m in result["matches"][:8]:
                    lines.append(
                        f"• ${m.get('amount')} — {m.get('description')} "
                        f"(id {m.get('id')})"
                    )
                lines.append(
                    "Say “void both $AMOUNT …” or pass a specific id."
                )
                return "\n".join(lines)
            return str(result["error"] if isinstance(result, dict) else result)
        if result.get("created") or result.get("voided"):
            return _write_result_message(intent["tool"], result)
        return str(result.get("message") or result)

    # Void/reverse expenses BEFORE create_expense — "void the $125…" must not
    # post a new expense named "void the $125…".
    if deterministic_reply is None and pending_plan is None:
        intent = _void_expense_intent(landlord, message)
        if intent is not None:
            # If several match and they didn't say both, still try amount-only
            # void_all when the message clearly wants the wrong duplicates gone.
            text_l = _landlord_words(message).casefold()
            if (
                not intent["arguments"].get("void_all")
                and "wrong" in text_l
                and intent["arguments"].get("amount")
            ):
                # Preview without void_all first; if multi-match, retry as all.
                first = execute(
                    intent["tool"], intent["arguments"], landlord=landlord,
                )
                if isinstance(first, dict) and first.get("matches"):
                    intent["arguments"]["void_all"] = "yes"
            deterministic_reply = _run_money_intent(intent)

    # Verbal cash expense with no receipt this turn. Pending receipt photos /
    # unscoped docs win unless the landlord explicitly says "no receipt".
    pending_doc_id = _pending_unscoped_document_id(landlord, conversation_id)
    focus_has_pending_file = _focus_has_pending_file(attachment_focus)
    receipt_followup = _looks_like_receipt_followup(message)
    if deterministic_reply is None and pending_plan is None:
        allow_verbal = True
        if (focus_has_pending_file or pending_doc_id) and not _NO_RECEIPT_RE.search(
            message or ""
        ):
            # Old pending receipt must not be abandoned by "$13 for McKenzie"
            # correction language — only explicit no-receipt / new cash log.
            allow_verbal = False
        if receipt_followup:
            allow_verbal = False
        if allow_verbal:
            intent = _verbal_expense_intent(landlord, message, safe_context)
            if intent is not None:
                deterministic_reply = _run_money_intent(intent)

    # Deterministic document routing: weak models repeatedly treated photographed
    # invoices as listing photos, or claimed OCR does not exist. Once intent is
    # a business record (or the file is a PDF/receipt), the backend itself
    # prepares hash+OCR first — address is only required after that.
    # Also parse the CURRENT message for upload_id= / attachment batch markers —
    # Telegram photos are always upload_id, never attachment_id.
    msg_upload_ids = re.findall(
        r"upload_id=([0-9a-fA-F-]{32,36})", message or ""
    )
    msg_attachment_ids = re.findall(
        r"(?:attachment_id=|items=\d+:)([0-9a-fA-F-]{32,36})", message or ""
    )
    # Do not re-open catalog for a prior upload when this turn is a verbal expense.
    verbal_this_turn = None
    if not receipt_followup and not (
        (focus_has_pending_file or pending_doc_id)
        and not _NO_RECEIPT_RE.search(message or "")
    ):
        verbal_this_turn = _verbal_expense_intent(landlord, message, safe_context)
    focus = attachment_focus or {}
    scope_query = _address_scope_from_message(message, safe_context)
    amount_correction = _amount_from_message(message)
    link_existing = _wants_link_existing_expense(message)
    # Follow-up after prepare: "950 McKenzie" / amount correction / "store as receipt"
    # even when the synthetic upload markers are no longer in the last user text.
    wants_new = _wants_new_expense_not_link(message)
    can_catalog = (
        bool(msg_upload_ids)
        or bool(msg_attachment_ids)
        or (
            focus.get("landlord_described_as_business_record")
            and focus_has_pending_file
        )
        or supported_tool_for_request(message) == "catalog_business_document"
        or (
            bool(pending_doc_id)
            and (
                bool(scope_query)
                or receipt_followup
                or link_existing
                or wants_new
                or bool(amount_correction)
            )
        )
        or (
            focus_has_pending_file
            and (
                bool(scope_query)
                or receipt_followup
                or link_existing
                or wants_new
            )
        )
    )
    if (
        deterministic_reply is None
        and pending_plan is None
        and verbal_this_turn is None
        and can_catalog
        and (
            attachment_focus
            or msg_upload_ids
            or msg_attachment_ids
            or pending_doc_id
        )
    ):
        focus = attachment_focus or {}
        batch_ids = list(focus.get("attachment_ids") or [])
        upload_ids = list(focus.get("unresolved_upload_ids") or [])
        doc_ids = list(focus.get("document_ids") or [])
        # Prefer live message markers (this turn's Telegram photo).
        for mid in msg_upload_ids:
            if mid not in upload_ids:
                upload_ids.append(mid)
        for mid in msg_attachment_ids:
            if mid not in batch_ids:
                batch_ids.append(mid)
        if pending_doc_id and pending_doc_id not in doc_ids:
            doc_ids.append(pending_doc_id)
        # Exactly one file handle this turn — pick the right tool arg name.
        if len(batch_ids) == 1 and not upload_ids and not doc_ids:
            file_arg = {"attachment_id": batch_ids[0]}
        elif len(upload_ids) == 1 and not batch_ids and not doc_ids:
            file_arg = {"upload_id": upload_ids[0]}
        elif len(doc_ids) == 1 and not batch_ids and not upload_ids:
            file_arg = {"document_id": doc_ids[0]}
        elif len(doc_ids) >= 1 and (scope_query or receipt_followup or link_existing):
            # Scope/amount follow-up: document_id is the right handle.
            file_arg = {"document_id": doc_ids[0]}
        elif len(upload_ids) == 1:
            # Mixed history: this turn's photo wins as upload.
            file_arg = {"upload_id": upload_ids[0]}
        elif len(batch_ids) == 1:
            file_arg = {"attachment_id": batch_ids[0]}
        else:
            file_arg = {}
        if file_arg:
            from rentium.rama.document_services import match_receipt_to_logged_expense
            from rentium.rama.models import RamaDocument as _RamaDoc

            # Caption context: this message + recent user words (e.g. "draino").
            caption_bits = [_landlord_words(message or "")]
            for row in RamaAudit.objects.filter(
                landlord=landlord,
                conversation_id=conversation_id,
                kind=RamaAudit.Kind.USER_MESSAGE,
            ).order_by("-created_at")[:6]:
                caption_bits.append(
                    _landlord_words(str((row.content or {}).get("text") or ""))
                )
            caption = " ".join(b for b in caption_bits if b)[:500]

            # Landlord amount correction before catalog so intelligence matches.
            target_doc = file_arg.get("document_id") or pending_doc_id
            if amount_correction and target_doc:
                _apply_document_amount_correction(
                    landlord, target_doc, amount_correction
                )

            # Match only when plausible — never force-link a $39 nozzle to $18 Draino.
            matched_expense = None
            force_new = _wants_new_expense_not_link(message)
            if (
                target_doc
                and not force_new
                and (link_existing or receipt_followup or not scope_query)
            ):
                doc_row = _RamaDoc.objects.filter(
                    pk=target_doc, landlord=landlord
                ).first()
                if doc_row is not None:
                    matched_expense = match_receipt_to_logged_expense(
                        landlord, doc_row, caption=caption
                    )
                    if matched_expense and amount_correction:
                        try:
                            from decimal import Decimal as _D

                            if abs(
                                _D(str(amount_correction))
                                - _D(str(matched_expense.get("amount") or "0"))
                            ) > _D("2.00"):
                                matched_expense = None
                        except Exception:  # noqa: BLE001
                            matched_expense = None
                    if matched_expense and amount_correction:
                        _apply_document_amount_correction(
                            landlord, target_doc, amount_correction
                        )
                    elif matched_expense and matched_expense.get("amount"):
                        # Prefer logged amount only when OCR gift-card noise.
                        _apply_document_amount_correction(
                            landlord,
                            target_doc,
                            matched_expense["amount"],
                        )

            # Scope from this message, recent messages, or matched expense.
            effective_scope = scope_query or ""
            if not effective_scope:
                for bit in caption_bits:
                    hit = _address_scope_from_message(bit, safe_context)
                    if hit:
                        effective_scope = hit
                        break
            if not effective_scope and matched_expense and matched_expense.get(
                "holding_address"
            ):
                effective_scope = matched_expense["holding_address"]

            arguments = {
                "scope_query": effective_scope,
                "issuer": focus.get("issuer") or "",
                "document_date": focus.get("document_date") or "",
                **file_arg,
            }
            # Auto-catalog when we know the holding.
            auto_scope = bool(effective_scope) and (
                receipt_followup
                or link_existing
                or force_new
                or bool(amount_correction)
                or bool(matched_expense)
                or bool(scope_query)
            )
            auto_link_now = bool(link_existing) and bool(
                effective_scope or matched_expense
            )
            if auto_scope or auto_link_now:
                arguments["confirm"] = "yes"
                if not arguments.get("scope_query") and effective_scope:
                    arguments["scope_query"] = effective_scope
            result = execute(
                "catalog_business_document", arguments, landlord=landlord,
            )
            if not isinstance(result, dict):
                result = {"error": str(result)}

            doc_id = str(
                result.get("document_id")
                or target_doc
                or (result.get("intelligence") or {}).get("document_id")
                or ""
            )
            if doc_id and not force_new:
                doc_row = _RamaDoc.objects.filter(
                    pk=doc_id, landlord=landlord
                ).first()
                if doc_row is not None:
                    rematch = match_receipt_to_logged_expense(
                        landlord, doc_row, caption=caption
                    )
                    if rematch and amount_correction:
                        try:
                            from decimal import Decimal as _D

                            if abs(
                                _D(str(amount_correction))
                                - _D(str(rematch.get("amount") or "0"))
                            ) > _D("2.00"):
                                rematch = None
                        except Exception:  # noqa: BLE001
                            rematch = None
                    matched_expense = rematch
                    intel = result.get("intelligence") or {}
                    if matched_expense:
                        intel = {**intel, "matching_expense": matched_expense}
                        if matched_expense.get("amount") and not amount_correction:
                            intel["amount"] = matched_expense["amount"]
                            _apply_document_amount_correction(
                                landlord,
                                doc_id,
                                matched_expense["amount"],
                            )
                    result["intelligence"] = intel
                    result["matching_expense"] = matched_expense
            elif force_new:
                matched_expense = None
                result["matching_expense"] = None

            # Link when landlord confirmed expense already logged, OR when we
            # matched uniquely and they accepted / auto path.
            should_link = (not force_new) and (
                bool(link_existing)
                or (auto_link_now and matched_expense is not None)
            )
            if (
                should_link
                and doc_id
                and (
                    result.get("catalogued")
                    or result.get("already_done")
                    or result.get("updated")
                    or result.get("ok")
                    or (matched_expense and effective_scope)
                )
            ):
                # Ensure holding is set before link.
                if (
                    matched_expense
                    and matched_expense.get("holding_address")
                    and not result.get("catalogued")
                ):
                    scoped = execute(
                        "catalog_business_document",
                        {
                            "document_id": doc_id,
                            "scope_query": matched_expense["holding_address"],
                            "confirm": "yes",
                        },
                        landlord=landlord,
                    )
                    if isinstance(scoped, dict) and not scoped.get("error"):
                        result = {**result, **scoped}
                link_args = {
                    "document_id": doc_id,
                    "amount": amount_correction
                    or (matched_expense or {}).get("amount")
                    or "",
                    "payment_state": "",
                    "duplicate_resolution": (
                        f"link:{matched_expense['expense_id']}"
                        if matched_expense and matched_expense.get("expense_id")
                        else "auto_link"
                    ),
                    "confirm": "yes",
                }
                link_result = execute(
                    "file_business_document", link_args, landlord=landlord,
                )
                if isinstance(link_result, dict) and not link_result.get("error"):
                    result = {**result, **link_result, "linked_existing": True}
                elif isinstance(link_result, dict):
                    result = {**result, "link_attempt": link_result}

            # Unique existing-expense match → confirm-link plan (not "which property?").
            if (
                not result.get("linked_existing")
                and not link_existing
                and not force_new
                and matched_expense
                and doc_id
                and (
                    result.get("needs_input")
                    or result.get("prepared")
                    or result.get("needs_confirm")
                    or result.get("catalogued")
                )
            ):
                if matched_expense.get("holding_address"):
                    execute(
                        "catalog_business_document",
                        {
                            "document_id": doc_id,
                            "scope_query": matched_expense["holding_address"],
                            "confirm": "yes",
                        },
                        landlord=landlord,
                    )
                save_single(
                    landlord,
                    conversation_id,
                    "file_business_document",
                    {
                        "document_id": doc_id,
                        "amount": matched_expense.get("amount") or "",
                        "duplicate_resolution": (
                            f"link:{matched_expense['expense_id']}"
                        ),
                    },
                )
                me = matched_expense
                deterministic_reply = (
                    f"This looks like the receipt for the existing expense:\n"
                    f"• ${me.get('amount')} — {me.get('description')}\n"
                    f"• Property: {me.get('holding_address') or 'portfolio'}\n"
                    f"OCR may also show other figures (e.g. gift cards); "
                    f"I will use the logged ${me.get('amount')}.\n"
                    f"Reply yes to store this receipt against that expense "
                    f"(no second expense), or no / “new expense” to file a "
                    f"separate cost instead."
                )
                result = {
                    **result,
                    "needs_confirm_link": True,
                    "matching_expense": me,
                }

            # No match (or landlord said new expense): file receipt as a NEW
            # ledger expense when we have amount + holding — one confirm, and
            # the receipt is attached (not a bare create_expense with no file).
            if (
                deterministic_reply is None
                and not result.get("linked_existing")
                and not matched_expense
                and doc_id
                and effective_scope
            ):
                from decimal import Decimal as _D
                from rentium.rama.models import RamaDocument as _RD

                doc_row = _RD.objects.filter(pk=doc_id, landlord=landlord).first()
                amt = amount_correction or ""
                if not amt and doc_row and doc_row.amount is not None:
                    amt = str(doc_row.amount)
                if amt:
                    # Ensure holding is set.
                    if not (doc_row and doc_row.holding_id):
                        execute(
                            "catalog_business_document",
                            {
                                "document_id": doc_id,
                                "scope_query": effective_scope,
                                "confirm": "yes",
                            },
                            landlord=landlord,
                        )
                    title = _receipt_title_from_caption(caption, amt)
                    # Category hint from caption.
                    cat = ""
                    low_cap = caption.casefold()
                    if re.search(
                        r"\b(nozzle|washer|repair|plumb|drain|screen|"
                        r"maintenance|fix|install)\b",
                        low_cap,
                    ):
                        cat = "MAINTENANCE"
                    paid = bool(
                        re.search(
                            r"\b(paid|already paid|from the bank|left the bank|"
                            r"taken from (the )?bank)\b",
                            caption,
                            re.I,
                        )
                    )
                    file_args = {
                        "document_id": doc_id,
                        "amount": amt,
                        "title": title,
                        "expense_category": cat,
                        "payment_state": "PAID" if paid else "UNPAID",
                        "duplicate_resolution": "new",
                    }
                    preview = execute(
                        "file_business_document", file_args, landlord=landlord,
                    )
                    if isinstance(preview, dict) and preview.get("needs_confirm"):
                        save_single(
                            landlord,
                            conversation_id,
                            "file_business_document",
                            file_args,
                        )
                        p = preview.get("preview") or {}
                        deterministic_reply = (
                            "New expense from this receipt:\n"
                            f"• Amount: ${p.get('amount') or amt}\n"
                            f"• Description: {p.get('title') or title}\n"
                            f"• Property: {p.get('holding') or effective_scope}\n"
                            f"• Category: {(p.get('expense_category') or cat or 'OTHER').replace('_', ' ')}\n"
                            f"• Bank: {'paid today' if paid else 'not yet taken from bank'}\n"
                            "Reply yes to file the receipt and post the expense, "
                            "or no to cancel."
                        )
                        result = {
                            **result,
                            "needs_confirm_new_expense": True,
                            **preview,
                        }
                    elif isinstance(preview, dict) and preview.get("filed"):
                        deterministic_reply = str(
                            preview.get("message")
                            or f"Filed receipt and posted ${amt}."
                        )
                        result = {**result, **preview}

            safe_result = json.loads(json.dumps(result, default=str))
            tools_used.append("catalog_business_document")
            audit(
                RamaAudit.Kind.TOOL_CALL,
                {
                    "tool": "catalog_business_document",
                    "arguments": arguments,
                    "result": safe_result,
                    "deterministic_routing": True,
                },
            )
            if result.get("needs_confirm_link"):
                pass  # reply already set
            elif result.get("needs_confirm"):
                save_single(
                    landlord,
                    conversation_id,
                    "catalog_business_document",
                    arguments,
                )
                deterministic_reply = _document_preview_reply(result)
            elif result.get("needs_input"):
                intel = result.get("intelligence") or {}
                bits = []
                if intel.get("kind_display") or intel.get("kind"):
                    bits.append(str(intel.get("kind_display") or intel.get("kind")))
                if intel.get("title"):
                    bits.append(str(intel["title"]))
                if intel.get("amount"):
                    bits.append(f"${intel['amount']}")
                cands = intel.get("amount_candidates") or []
                if cands and len(cands) > 1:
                    alt = ", ".join(
                        f"${c.get('amount')}" for c in cands[:4] if c.get("amount")
                    )
                    if alt:
                        bits.append(f"also saw {alt}")
                summary = (
                    ("I read this as " + " · ".join(bits) + ". ") if bits else ""
                )
                if result.get("status") == "FAILED" or (
                    intel.get("status") == "FAILED"
                ):
                    deterministic_reply = (
                        summary
                        + "OCR hit a processing error on the server (not a blurry "
                        "photo). I will retry OCR; if it still fails, use Retry on "
                        "the Documents page. "
                        + str(result.get("question_for_user") or "")
                    ).strip()
                else:
                    me = result.get("matching_expense") or intel.get(
                        "matching_expense"
                    )
                    if me:
                        deterministic_reply = (
                            summary
                            + f"This matches the logged expense "
                            f"${me.get('amount')} — {me.get('description')} "
                            f"at {me.get('holding_address') or 'portfolio'}. "
                            f"Reply yes to store the receipt against that expense "
                            f"(no second post), or name a different property."
                        ).strip()
                        save_single(
                            landlord,
                            conversation_id,
                            "file_business_document",
                            {
                                "document_id": doc_id,
                                "amount": me.get("amount") or "",
                                "duplicate_resolution": (
                                    f"link:{me['expense_id']}"
                                ),
                            },
                        )
                    else:
                        deterministic_reply = (
                            summary
                            + str(
                                result.get("question_for_user")
                                or (
                                    "Which physical property address does this "
                                    "belong to? (Or whole portfolio.)"
                                )
                            )
                        ).strip()
            elif result.get("filed") or result.get("linked_existing"):
                det = result.get("message") or result.get("relay_instruction") or ""
                if result.get("ledger_entry_id") or result.get("linked_existing"):
                    deterministic_reply = (
                        det
                        or (
                            "Stored the receipt and linked it to the existing "
                            f"expense"
                            + (
                                f" (${result.get('amount')})"
                                if result.get("amount")
                                else ""
                            )
                            + " — no second expense posted."
                        )
                    )
                else:
                    deterministic_reply = det or "Document filed."
            elif result.get("already_done") or result.get("is_duplicate"):
                # Already in library — still try link if they said already logged.
                if link_existing and doc_id and not result.get("linked_existing"):
                    link_result = execute(
                        "file_business_document",
                        {
                            "document_id": doc_id,
                            "amount": amount_correction
                            or (matched_expense or {}).get("amount")
                            or "",
                            "duplicate_resolution": "auto_link",
                            "confirm": "yes",
                        },
                        landlord=landlord,
                    )
                    if isinstance(link_result, dict) and (
                        link_result.get("filed") or link_result.get("linked_existing")
                    ):
                        deterministic_reply = str(
                            link_result.get("message")
                            or "Receipt linked to the existing expense."
                        )
                    else:
                        deterministic_reply = str(
                            (link_result or {}).get("error")
                            or result.get("message")
                            or "This file is already in your document library."
                        )
                else:
                    deterministic_reply = str(
                        result.get("message")
                        or result.get("relay_instruction")
                        or "This file is already in your document library."
                    )
            elif result.get("error"):
                deterministic_reply = str(result["error"])
            elif result.get("catalogued"):
                intel = result.get("intelligence") or {}
                amt = (
                    amount_correction
                    or (matched_expense or {}).get("amount")
                    or intel.get("amount")
                )
                hold = (
                    (result.get("holding") or {}).get("address")
                    or (matched_expense or {}).get("holding_address")
                    or effective_scope
                )
                if result.get("link_attempt") and not result.get("linked_existing"):
                    deterministic_reply = (
                        f"Stored the receipt under {hold}"
                        + (f" at ${amt}" if amt else "")
                        + ". "
                        + str(
                            result["link_attempt"].get("error")
                            or result["link_attempt"].get("question_for_user")
                            or "Could not auto-link the expense — say which one."
                        )
                    )
                elif matched_expense and not result.get("linked_existing"):
                    save_single(
                        landlord,
                        conversation_id,
                        "file_business_document",
                        {
                            "document_id": doc_id,
                            "amount": amt or "",
                            "duplicate_resolution": (
                                f"link:{matched_expense['expense_id']}"
                            ),
                        },
                    )
                    deterministic_reply = (
                        f"Stored under {hold}"
                        + (f" · ${amt}" if amt else "")
                        + f". This matches ${matched_expense.get('amount')} — "
                        f"{matched_expense.get('description')}. "
                        f"Reply yes to attach the receipt to that expense "
                        f"(no second post)."
                    )
                else:
                    deterministic_reply = (
                        f"Stored as a business document for {hold}"
                        + (f" · ${amt}" if amt else "")
                        + ". "
                        + (
                            "Linked to the existing expense — no new ledger row."
                            if result.get("linked_existing")
                            else (
                                "If the expense is already on the books, say so "
                                "and I will attach this receipt without posting "
                                "again; otherwise say paid/unpaid to file it."
                            )
                        )
                    )
            elif result.get("prepared"):
                deterministic_reply = _document_preview_reply(result)
            elif result.get("ok") and link_existing and not result.get(
                "linked_existing"
            ):
                # Status-only return (document_id path without scope) after
                # "expense is already logged" — try match + link.
                if matched_expense and doc_id:
                    if matched_expense.get("holding_address"):
                        execute(
                            "catalog_business_document",
                            {
                                "document_id": doc_id,
                                "scope_query": matched_expense["holding_address"],
                                "confirm": "yes",
                            },
                            landlord=landlord,
                        )
                    link_result = execute(
                        "file_business_document",
                        {
                            "document_id": doc_id,
                            "amount": amount_correction
                            or matched_expense.get("amount")
                            or "",
                            "duplicate_resolution": (
                                f"link:{matched_expense['expense_id']}"
                            ),
                            "confirm": "yes",
                        },
                        landlord=landlord,
                    )
                    if isinstance(link_result, dict) and (
                        link_result.get("filed") or link_result.get("linked_existing")
                    ):
                        deterministic_reply = str(
                            link_result.get("message")
                            or "Receipt linked to the existing expense."
                        )
                    else:
                        deterministic_reply = str(
                            (link_result or {}).get("error")
                            or "Could not link the receipt to the logged expense."
                        )
                else:
                    deterministic_reply = (
                        "I have the receipt but could not uniquely match a logged "
                        "expense. Name the amount or property (e.g. Draino $13.41 "
                        "at McKenzie)."
                    )

    if (
        deterministic_reply is None
        and pending_plan is None
        and _document_location_request(message)
    ):
        document_id = _recent_document_id(landlord, conversation_id, message)
        if document_id:
            result = execute(
                "business_document_location",
                {"document_id": document_id},
                landlord=landlord,
            )
            safe_result = json.loads(json.dumps(result, default=str))
            tools_used.append("business_document_location")
            audit(
                RamaAudit.Kind.TOOL_CALL,
                {
                    "tool": "business_document_location",
                    "arguments": {"document_id": document_id},
                    "result": safe_result,
                    "deterministic_routing": True,
                },
            )
            deterministic_reply = (
                str(result["error"])
                if result.get("error")
                else _document_location_reply(result)
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
            attachments=turn_attachments,
            auto_executed=delegated_auto,
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
                },
            )
            for call in turn.tool_calls:
                effective_arguments = _contextualize_tool_arguments(
                    call.name, call.arguments, focus,
                )
                if call.name == "create_group_room":
                    effective_arguments = _enrich_empty_group_room_arguments(
                        landlord,
                        conversation_id,
                        effective_arguments,
                    )
                original_property_query = str(
                    effective_arguments.get("property_query") or "",
                ).strip()
                alias_id = planned_property_aliases.get(
                    " ".join(original_property_query.casefold().split()),
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
                    for receipt in result.get("auto_executed") or []:
                        if receipt not in delegated_auto:
                            delegated_auto.append(receipt)
                else:
                    result = execute(call.name, effective_arguments, landlord=landlord)
                # Before this preview can become a proposal: is it already done?
                # A preview is side-effect free, so replacing it here costs
                # nothing and means the duplicate is never shown at all.
                duplicate = _refuse_if_already_done(
                    call.name, effective_arguments, result, landlord,
                )
                if duplicate is not None:
                    result = duplicate
                # JSON-safe for audit + tool message content (UUIDs, Decimals).
                safe_result = json.loads(json.dumps(result, default=str))
                if isinstance(result, dict) and isinstance(
                    result.get("_attachment"), dict,
                ):
                    turn_attachments.append(result["_attachment"])
                tools_used.append(call.name)
                if _is_write_result(result):
                    turn_writes.append(call.name)
                audit(
                    RamaAudit.Kind.TOOL_CALL,
                    {
                        "tool": call.name,
                        "arguments": effective_arguments,
                        "result": safe_result,
                    },
                )
                if isinstance(result, dict) and result.get("needs_confirm"):
                    # A preview's warnings must reach the landlord verbatim.
                    # For a SINGLE write the reply is the model's own prose
                    # (only batches get a deterministic renderer), so a
                    # duplicate_warning / double_count_warning / overpayment
                    # _warning sitting in the payload could simply be left out
                    # of the sentence they actually read. Collected generically
                    # on `*_warning` so a new one is carried without anybody
                    # remembering to wire it.
                    for key, value in (result.get("preview") or {}).items():
                        if key.endswith("_warning") and str(value or "").strip():
                            text = str(value).strip()
                            if text not in preview_warnings:
                                preview_warnings.append(text)
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
                                effective_arguments.get("name") or "",
                            ).strip()
                        ):
                            stable_arguments["property_query"] = str(
                                preview["id"],
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
                            or "",
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
                            effective_arguments.get("name") or "",
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
                        {"target": target, "error": str(result["error"])},
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
                    },
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
                ),
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

    # ------------------------------------------------- the autonomy gate
    # The ONLY place a preview can become an execution without the landlord
    # saying yes. Everything above is unchanged: the model previewed, and its
    # `confirm` was blanked. What follows is the identical path a landlord's
    # "yes" takes, with the yes supplied by a Constitution rule they confirmed.
    auto_executed: list[dict] = []
    auto = autonomy_policy.evaluate_turn(
        landlord,
        pending_specs,
        role=role,
        channel=channel,
        had_pending_plan=pending_plan is not None or plan_progress is not None,
    )
    if auto.approved:
        auto_plan = save_batch(landlord, conversation_id, pending_specs)

        def _auto_audit(content):
            tools_used.append(content.get("tool", ""))
            audit(RamaAudit.Kind.TOOL_CALL, {**content, "autonomous": True})

        errors = validate_plan(plan_to_payload(auto_plan)["steps"], landlord)
        if errors:
            # A blocker appeared between preview and now. Fall back to asking
            # rather than executing something that no longer validates.
            auto = autonomy_policy.AutonomyDecision(False, "; ".join(errors), auto.policy)
        else:
            progress = run_plan(auto_plan, landlord, audit=_auto_audit)
            auto_executed = autonomy_policy.record_auto_actions(
                landlord, conversation_id, progress, auto.policy,
            )
            # The model's "reply yes to confirm" prose described a proposal
            # that has now already happened, so it is discarded exactly as the
            # confirmed-yes path discards it.
            reply = _plan_fallback_reply(progress) + _undo_hint(auto_executed)
            audit(
                RamaAudit.Kind.ASSISTANT_MESSAGE,
                {
                    "text": reply,
                    "tools_used": tools_used,
                    "deterministic": True,
                    "auto_executed": [item["id"] for item in auto_executed],
                    "policy_rule_id": auto.policy.rule_id if auto.policy else None,
                },
            )
            outstanding = load_fresh_plan(landlord, conversation_id)
            return TurnResult(
                conversation_id=conversation_id,
                reply=reply,
                provider=provider_name,
                model=model,
                tools_used=tools_used,
                attachments=turn_attachments,
                pending_plan=plan_brief(outstanding) if outstanding else None,
                deterministic=True,
                auto_executed=delegated_auto + auto_executed,
            )

    # ---- turn contract: a promise is not an answer ------------------------
    # `if not turn.tool_calls: break` treats ANY tool-call-free turn as
    # finished, so "Checking the ledger now." shipped as a complete reply. One
    # continuation round is pushed back through the model before the landlord
    # is made to ask "and?". Only on the stall path, so the ordinary turn costs
    # nothing extra.
    if (
        not pending_specs
        and turn.text
        and promises_without_delivering(turn.text, tools_used)
    ):
        audit(RamaAudit.Kind.ERROR, {"error": "promised_without_delivering",
                                     "text": turn.text[:500]})
        messages.append({"role": "assistant", "text": turn.text})
        messages.append({"role": "user", "text": _DELIVER_NOW})
        try:
            follow_up = provider.complete(
                model=model,
                system=system,
                messages=messages,
                tools=turn_tools,
                api_key=api_key,
            )
        except ProviderError:
            follow_up = None
        if follow_up is not None and not follow_up.tool_calls:
            # Took the nudge and answered in prose — use it.
            if (follow_up.text or "").strip():
                turn = follow_up
        elif follow_up is not None:
            # It wants tools. Run them, then let it speak once more.
            for call in follow_up.tool_calls:
                args = dict(call.arguments or {})
                registered = REGISTRY.get(call.name)
                if registered is not None and "confirm" in registered.parameters.get(
                    "properties", {},
                ):
                    # The confirm invariant holds here exactly as in the main
                    # loop: a model may prepare a write, never approve one.
                    args["confirm"] = ""
                result = execute(call.name, args, landlord=landlord)
                tools_used.append(call.name)
                audit(
                    RamaAudit.Kind.TOOL_CALL,
                    {"tool": call.name, "arguments": args,
                     "result": json.loads(json.dumps(result, default=str))},
                )
                messages.append(
                    {
                        "role": "assistant",
                        "text": follow_up.text,
                        "tool_calls": [
                            {
                                "id": call.id,
                                "name": call.name,
                                "arguments": args,
                            },
                        ],
                    },
                )
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": call.id,
                        "name": call.name,
                        "content": json.dumps(
                            json.loads(json.dumps(result, default=str)),
                        ),
                    },
                )
            try:
                final = provider.complete(
                    model=model,
                    system=system,
                    messages=messages,
                    tools=turn_tools,
                    api_key=api_key,
                )
                if (final.text or "").strip():
                    turn = final
            except ProviderError:
                pass

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

    # If it stalled again, do not ship a second promise. Say what the turn's
    # tools actually found, or admit the stall — either beats making the
    # landlord prompt for an answer a third time.
    if promises_without_delivering(reply, tools_used):
        audit(RamaAudit.Kind.ERROR, {"error": "promised_twice", "text": reply[:500]})
        facts = _tool_facts_note(landlord, conversation_id)
        response_deterministic = True
        reply = (
            "Here's what I have, rather than another promise to go and look:\n"
            + "\n".join(f"• {line}" for line in facts[-4:])
            if facts
            else (
                "I said I'd check and then didn't — that's my fault, not "
                "something you should have to chase. Ask me once more and "
                "I'll give you the answer instead of the intention."
            )
        )
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

    # A model cannot create a completed write with prose either. The General,
    # asked to record $100 of a $425 deposit, replied "Recorded the $100
    # payment against the deposit charge for Room C" having called nothing but
    # a read — so the landlord believed the money was on the books for days.
    #
    # This is the worst failure the engine can have: every other kind of wrong
    # answer is visibly wrong, and this one looks exactly like success. So the
    # claim is refused whenever nothing was actually written.
    #
    # Note this no longer skips when a plan is outstanding. A pending preview
    # for one thing must not excuse a false claim about another — "I recorded
    # the payment; shall I also update the rent?" is precisely the shape where
    # a proposal and a fabrication travel in the same message. A truthful
    # preview says "I'll record", which is future tense and never matches.
    if (
        not response_deterministic
        and claims_completed_write(reply)
        and not _turn_wrote_anything(turn_writes, delegated_auto)
    ):
        response_deterministic = True
        audit(
            RamaAudit.Kind.ERROR,
            {"error": "claimed_write_without_writing", "claim": reply[:500]},
        )
        gap = _capability_gap_hint(landlord, message, conversation_id)
        reply = (
            "I said that as though it were done — it isn't. Nothing was "
            "written, so please don't rely on that.\n\n" + gap
        )

    # A warning the tool computed is not advice the model may edit out. Only
    # appended when the reply does not already carry it, so a model that DID
    # relay it is not made to repeat itself.
    if outstanding is not None and preview_warnings:
        missing = [w for w in preview_warnings if w not in reply]
        if missing:
            reply = reply.rstrip() + "\n\n" + "\n\n".join(f"⚠️ {w}" for w in missing)

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
        auto_executed=delegated_auto,
    )
