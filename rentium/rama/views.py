"""
RAMA v1: a read-only Q&A agent over the landlord's own portfolio.

Preferences (enabled / provider / model / optional BYOK api_key) are
per-landlord. Chat memory is rebuilt from this landlord's RamaAudit rows.
"""

import json
import uuid

from rest_framework import status as http_status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.exceptions import PermissionDenied
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from django.utils import timezone

from .models import RamaAudit, RamaPendingAction, RamaPreferences
from .providers import PROVIDERS, ProviderError, Turn, get_provider
from .registry import execute, tool_schemas
from .runtime import (
    MODEL_CATALOG,
    get_landlord_config,
    platform_api_key,
    resolve_model,
)
from .union import live_context, state_of_the_union

MAX_TOOL_ROUNDS = 20  # multi-step room/lease/invite needs headroom
HISTORY_TURNS = 12
MAX_MESSAGE_CHARS = 6000
# A previewed write is honored as "the thing the landlord just said yes to" only
# while it's fresh; after this it's stale and the model must re-preview.
PENDING_ACTION_TTL_SECONDS = 30 * 60

# Bare affirmations that mean "run the action you just previewed". The confirm
# step is executed by the backend from the persisted pending action, so a weak
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


def _load_fresh_pending(landlord, conversation_id):
    """The still-valid previewed action for this conversation, or None."""
    pending = RamaPendingAction.objects.filter(
        conversation_id=conversation_id, landlord=landlord
    ).first()
    if pending is None:
        return None
    if (timezone.now() - pending.created_at).total_seconds() > PENDING_ACTION_TTL_SECONDS:
        pending.delete()
        return None
    return pending


def _save_pending(landlord, conversation_id, pending_call):
    """Upsert the outstanding preview, or clear it when the turn resolved cleanly.

    Invariant: a pending row exists IFF the last turn ended on an unconfirmed
    preview — so on the next 'yes' the backend knows exactly what to run.
    """
    if pending_call:
        RamaPendingAction.objects.update_or_create(
            conversation_id=conversation_id,
            defaults={
                "landlord": landlord,
                "tool": pending_call["tool"],
                "arguments": pending_call["arguments"],
                "preview": pending_call.get("preview") or {},
            },
        )
    else:
        RamaPendingAction.objects.filter(
            conversation_id=conversation_id, landlord=landlord
        ).delete()


def _write_label(result: dict) -> str:
    for key in ("property", "lease", "group", "work_order"):
        obj = result.get(key)
        if isinstance(obj, dict):
            return str(obj.get("name") or obj.get("lease_number") or obj.get("id") or "").strip()
    return ""


def _fallback_reply(tool: str, result: dict) -> str:
    """Deterministic summary used only if the model can't be reached AFTER a
    confirmed action already ran — so the landlord never sees an error for work
    that actually succeeded."""
    if not isinstance(result, dict):
        return "Done."
    if result.get("error"):
        return f"That action didn't go through: {result['error']}"
    if result.get("workflow") and (result.get("done") or result.get("steps_done")):
        label = result.get("property_name") or _write_label(result)
        return f"Done — full room setup completed. {label}".strip()
    verb = (
        "Created" if result.get("created")
        else "Deleted" if result.get("deleted")
        else "Updated" if result.get("updated")
        else "Done —"
    )
    label = _write_label(result) or (result.get("property") if result.get("deleted") else "")
    return f"{verb} {label}".strip() or "Done."


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

SYSTEM_PROMPT = """\
You are RAMA, the assistant inside Rentium, a Canadian property-management \
app. You work for exactly one landlord and can see only their portfolio.

HARD RULES (breaking these is a failure):
1) Every number, date, name, lease_number, and expense description MUST be \
copied from LIVE PORTFOLIO (below) or a tool result in THIS turn. Never invent \
totals (e.g. $4850 rent, $5000 deposits, property tax) that do not appear there.
2) LIVE PORTFOLIO is refreshed every message and OVERRIDES earlier chat turns \
that disagree (including your own past answers).
3) dashboard_truth is ground truth for portfolio totals — copy it exactly.
4) Never flip "has a signed lease" to "no lease" when LIVE PORTFOLIO shows \
lease_number / rented_or_committed_listings for that room.
5) Prefer LIVE PORTFOLIO + domain_digest first; call tools for detail.
6) Yes/No ONLY when the user asked a yes/no question. Do not start with \
"Yes." on "what/which/list/when" questions.
7) outstanding_total is unpaid due on or before as_of. next_charge and \
charge_schedule "scheduled" lines are FUTURE — not outstanding yet.
8) draft_leases: Draft ≠ rented. Say drafts exist if draft_lease_count > 0.
9) Viewings: copy date + weekday + time_display. Never invent weekdays.
10) Empty tools (0 work orders, 0 inquiries) → say none. Never invent records.
11) domain_digest + inventory_hint: if inventory_items_private/shared > 0, \
there IS furniture — call list_inventory; never say none recorded.
12) charge_schedule: status=scheduled is NOT portfolio outstanding; use due_now.

Routing:
- Listings/layout/occupancy → LIVE PORTFOLIO / list_properties / occupancy_as_of
- Leases/agreement/number → list_leases / lease_state
- Money totals → dashboard_truth; expenses → list_expenses; schedule by \
property → charge_schedule; one lease month → charge_status
- Viewings → list_appointments
- Work orders → list_work_orders (strong) or open_work_orders
- Inquiries/leads → list_inquiries
- Messages/threads → list_conversations then list_messages
- Condition inspections → list_inspections; attention still flags missing ones
- Move-in/out → list_move_events (lease start/end + move-out requests)
- Furniture/inventory → list_inventory
- Tenants & history → list_tenants / tenant_history (people across leases)
- Documents/PDFs metadata → list_documents (titles only, no file contents)
- Attention → attention_items

Wording:
- Same household unit: rooms in the SAME layout.groups[].listings → Yes.
- Garden Suite vs rooms → separate unit.
- Property type → primary_type (Garden Suite / Private Room).
- Standup: occupied X/Y (not "X/Y vacant" unless you mean vacant count).
- Be brief, accurate, complete enough for a landlord UI and a person chatting.

WRITE ACTIONS (L4 — confirmed only; same business rules as the UI):
- ALWAYS call once WITHOUT confirm first → show the needs_confirm preview, then STOP
  and wait. Do NOT call the same tool again in the same turn.
- The system handles the landlord's "yes" for you: when they approve, the previewed
  action is executed automatically and you'll get an "ACTION JUST EXECUTED" note —
  just report that outcome. Never re-preview or re-run an action already executed.
- Never invent success if a tool returned needs_confirm or error.
- Unsure which write tool? Call crud_capabilities.

PROPERTIES: create_property / update_property / delete_property. \
create_property_group / assign_property_to_group (rooms only). \
Delete blocked if any lease references the listing (PROTECT). \
Complete units cannot join groups; rooms need room_type; units need unit_type.

LEASES: create_lease (always DRAFT; type auto room→Roommate, BC unit→RTB-1). \
Defaults for landlord protection: smoking_allowed=false, pets_allowed=false, \
pet_deposit=0, cleaning_fee=0 unless the landlord sets them. \
security_deposit: if landlord said a deposit amount, pass it; if they only set \
pet/cleaning to 0, KEEP security deposit from earlier in the chat OR omit so \
it defaults to half of total_rent ($800 rent → $400). Pass security_deposit="0" \
ONLY when they explicitly want zero security deposit. \
update_lease only if not locked (ACTIVE/PENDING_SIGNATURES may lock fields — \
never rewrite signed ACTIVE leases). \
delete_draft_lease = DRAFT only; else terminate_lease (voids open charges). \
landlord_sign_lease requires fully allocated rent. \
Roster: list_lease_roster first. ADD roommate → add_roommate_to_lease (never replace). \
REPLACE invite → replace_lease_invite. CANCEL → cancel_lease_invite (rebalances rent). \
total_rent = unit rent; unsigned tenants share equally ($1000/2 → $500 each). \
Lease PDF: call lease_pdf_info — PDF is ALWAYS downloadable via UI /api/leases/<id>/pdf/ \
even if document_file is empty. NEVER say "no PDF exists" for an existing lease.

MULTI-STEP ROOM SETUP (be smart — do not drop steps):
When the landlord asks for a room together with any of furniture / lease / rent /
deposit / tenant / inspection in one request, ALWAYS use setup_room_tenancy — ONE
tool, ONE preview, ONE confirm runs the whole package (room → inventory → DRAFT
lease → invite tenant → move-in condition inspection). Do NOT hand-run
create_property then create_lease then invite separately for a combined request —
that is what previously dropped steps and created duplicates. Pass every detail the
landlord gave (address, city, group_name, inventory_items, start_date/end_date,
total_rent, security_deposit, tenant_name, tenant_email, special_terms) in that
single call. After the landlord confirms, the full chain runs — including the invite
link and the inspection — so answer "yes it will invite the tenant and set up the
inspection" when asked.
Otherwise (a genuinely single-step request):
1) create_property with inventory_items (e.g. "Single bed, Mattress") in SAME call
2) create_lease with total_rent, security_deposit (or half-month default), special_terms
3) invite_tenant_to_lease / add_roommate
4) create_condition_inspection (NOT schedule_viewing) after tenant exists
DUPLICATE NAMES: never create a second listing with the same name. If candidates
list returns multiple, pass property_query=<id> or pick=first|with_group|no_group.
To delete a duplicate: delete_property property_query=<id> confirm=yes (delete
draft leases first if PROTECT blocks). Do not rename/reassign in a loop.

MAINTENANCE: create_work_order; update_work_order for fields; \
transition_work_order or complete_work_order for status. NEVER delete WOs — cancel only. \
add_work_order_comment for notes. complete_work_order can post_expense.

INVENTORY: create_property.inventory_items OR bulk_add_inventory OR \
create/update/delete_inventory_item (private); shared on groups. \
"What's in it" empty = you forgot inventory tools.

OTHER: mark_inquiry_replied, send_tenant_message, mark_messages_read, \
schedule_viewing (SHOWINGS ONLY), create_condition_inspection (move-in/out reports), \
create_expense — all confirm-first.

Inspections: create_condition_inspection uses build_inspection (Condition Inspections panel). \
schedule_viewing is ONLY for prospective showings under Appointments. \
checklist_by_section has per-room line items. \
domain_digest.inspection_attention_list = items needing attention (never say none \
if that list is non-empty). Unread: unread_messages count. Documents: titles + files. \
HIGH priority WOs: high_or_emergency_work_orders / open_work_order_list. \
Lease ends: upcoming_lease_ends (e.g. Dec 31) — do not invent a 60-day cutoff.

Amounts in CAD. If a tool returns error, explain plainly."""


def _landlord(request):
    profile = getattr(request.user, "landlord_profile", None)
    if profile is None:
        raise PermissionDenied("Landlords only.")
    return profile


def _settings_payload(landlord, prefs: RamaPreferences | None = None) -> dict:
    prefs = prefs or RamaPreferences.for_landlord(landlord)
    cfg = get_landlord_config(landlord)
    return {
        "enabled": prefs.enabled,
        "provider": prefs.provider,
        "model": prefs.model or cfg.model,
        "has_api_key": bool((prefs.api_key or "").strip()),
        "configured": cfg.is_configured(),
        "providers": sorted(PROVIDERS),
        "models": MODEL_CATALOG,
        # Provider is always selectable; "ready" means BYOK or platform key.
        "platform_ready": {
            name: bool(
                (prefs.api_key or "").strip()
                if name == prefs.provider
                else platform_api_key(name)
            )
            or bool(platform_api_key(name))
            or name == prefs.provider  # always allow current + BYOK path
            for name in PROVIDERS
        },
        # All providers available when landlord can paste a key.
        "byok": True,
    }


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def union_view(request):
    """GET /api/rama/state-of-the-union/ — works without RAMA enabled."""
    return Response(state_of_the_union(_landlord(request)))


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def config_view(request):
    """GET /api/rama/config/ — panel payload for this landlord."""
    landlord = _landlord(request)
    cfg = get_landlord_config(landlord)
    return Response(
        {
            "enabled": cfg.enabled,
            "configured": cfg.is_configured(),
            "provider": cfg.provider,
            "model": cfg.model,
            "has_api_key": cfg.has_own_key,
            "providers": sorted(PROVIDERS),
            "models": MODEL_CATALOG,
            "can_override": False,
            "byok": True,
            "platform_ready": {
                name: bool(platform_api_key(name)) for name in PROVIDERS
            },
        }
    )


@api_view(["GET", "PATCH"])
@permission_classes([IsAuthenticated])
def settings_view(request):
    """GET/PATCH /api/rama/settings/ — enable, model, and optional API key."""
    landlord = _landlord(request)
    prefs = RamaPreferences.for_landlord(landlord)

    if request.method == "GET":
        return Response(_settings_payload(landlord, prefs))

    data = request.data or {}
    if "enabled" in data:
        prefs.enabled = bool(data.get("enabled"))

    provider = data.get("provider")
    if provider is not None:
        provider = str(provider).strip().lower()
        if provider not in PROVIDERS:
            return Response(
                {"detail": f"Unknown provider {provider!r}."},
                status=http_status.HTTP_400_BAD_REQUEST,
            )
        prefs.provider = provider

    if "model" in data:
        prefs.model = str(data.get("model") or "").strip()

    # api_key: blank string means "keep existing"; explicit null clears.
    if "api_key" in data:
        raw = data.get("api_key")
        if raw is None:
            prefs.api_key = ""
        else:
            raw = str(raw).strip()
            if raw:
                prefs.api_key = raw
            # empty string → keep existing (don't wipe on accidental blank save)

    if "clear_api_key" in data and data.get("clear_api_key"):
        prefs.api_key = ""

    prefs.save()
    return Response(_settings_payload(landlord, prefs))


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def chat_view(request):
    """POST /api/rama/chat/ {message, conversation_id?}"""
    landlord = _landlord(request)
    cfg = get_landlord_config(landlord)

    if not cfg.enabled:
        return Response(
            {
                "detail": "RAMA is turned off for your account. "
                "Enable it under Account → RAMA."
            },
            status=http_status.HTTP_403_FORBIDDEN,
        )
    if not cfg.is_configured():
        return Response(
            {
                "detail": (
                    "Add your API key under Account → RAMA (e.g. an xAI Grok key), "
                    "then try again."
                )
            },
            status=http_status.HTTP_503_SERVICE_UNAVAILABLE,
        )

    message = str(request.data.get("message") or "").strip()
    if not message:
        return Response(
            {"detail": "message is required."},
            status=http_status.HTTP_400_BAD_REQUEST,
        )
    if len(message) > MAX_MESSAGE_CHARS:
        return Response(
            {"detail": f"message is limited to {MAX_MESSAGE_CHARS} characters."},
            status=http_status.HTTP_400_BAD_REQUEST,
        )

    raw_conversation = request.data.get("conversation_id")
    try:
        conversation_id = (
            uuid.UUID(str(raw_conversation)) if raw_conversation else uuid.uuid4()
        )
    except ValueError:
        return Response(
            {"detail": "conversation_id must be a UUID."},
            status=http_status.HTTP_400_BAD_REQUEST,
        )

    provider_name = cfg.provider
    model = cfg.model
    api_key = cfg.api_key

    def audit(kind, content):
        RamaAudit.objects.create(
            landlord=landlord,
            conversation_id=conversation_id,
            kind=kind,
            provider=provider_name,
            model=model,
            content=content,
        )

    try:
        provider = get_provider(provider_name)
    except ProviderError as exc:
        return Response(
            {"detail": str(exc)}, status=http_status.HTTP_400_BAD_REQUEST
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
    system = (
        SYSTEM_PROMPT
        + "\n\n## LIVE PORTFOLIO (authoritative — overrides chat history)\n"
        + json.dumps(safe_context, indent=None, separators=(",", ":"))
    )
    done_notes = _recent_writes_note(landlord, conversation_id)
    if done_notes:
        system += (
            "\n\n## ALREADY DONE THIS CONVERSATION (do not repeat or re-create)\n"
            + "; ".join(done_notes)
        )
    audit(
        RamaAudit.Kind.TOOL_CALL,
        {
            "tool": "_live_context",
            "arguments": {},
            "result": safe_context,
        },
    )

    schemas = tool_schemas()
    tools_used: list[str] = ["_live_context"]
    turn = Turn()

    # Deterministic confirm: if the landlord just said "yes" to a previewed
    # write, run that exact tool ourselves — never rely on the model to
    # reconstruct it (that was the source of the endless re-preview loop).
    just_executed: dict | None = None
    pending = _load_fresh_pending(landlord, conversation_id)
    if pending is not None and _is_affirmative(message):
        run_args = dict(pending.arguments or {})
        run_args["confirm"] = "yes"
        confirmed_result = execute(pending.tool, run_args, landlord=landlord)
        safe_confirmed = json.loads(json.dumps(confirmed_result, default=str))
        tools_used.append(pending.tool)
        audit(
            RamaAudit.Kind.TOOL_CALL,
            {
                "tool": pending.tool,
                "arguments": run_args,
                "result": safe_confirmed,
                "auto_confirmed": True,
            },
        )
        just_executed = {"tool": pending.tool, "result": safe_confirmed}
        pending.delete()
        system += (
            "\n\n## ACTION JUST EXECUTED (landlord confirmed — already done; report "
            "the outcome, do NOT preview or run it again)\n"
            + json.dumps(just_executed, default=str)
        )

    # The still-outstanding preview at the end of the turn, if any. Persisted so
    # the next "yes" runs deterministically instead of re-previewing.
    pending_call: dict | None = None
    # A bare "yes" whose action we already ran needs no tools — just narrate it,
    # so a weak model can't re-run the write. "yes and delete X" keeps tools.
    turn_tools = schemas
    if just_executed is not None and _norm_affirm(message) in _AFFIRM_EXACT:
        turn_tools = []
    try:
        for _ in range(MAX_TOOL_ROUNDS):
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
                result = execute(call.name, call.arguments, landlord=landlord)
                # JSON-safe for audit + tool message content (UUIDs, Decimals).
                safe_result = json.loads(json.dumps(result, default=str))
                tools_used.append(call.name)
                audit(
                    RamaAudit.Kind.TOOL_CALL,
                    {
                        "tool": call.name,
                        "arguments": call.arguments,
                        "result": safe_result,
                    },
                )
                if isinstance(result, dict) and result.get("needs_confirm"):
                    pending_call = {
                        "tool": call.name,
                        "arguments": call.arguments or {},
                        "preview": result.get("preview") or {},
                    }
                elif isinstance(result, dict) and (
                    result.get("created")
                    or result.get("updated")
                    or result.get("deleted")
                    or result.get("done")
                ):
                    # A write went through — clear any matching outstanding
                    # preview so we don't ask the landlord to confirm it again.
                    if pending_call and pending_call["tool"] == call.name:
                        pending_call = None
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
        # If a confirmed action already ran, don't surface an error for work
        # that actually succeeded — report it deterministically instead.
        if just_executed is not None:
            _save_pending(landlord, conversation_id, pending_call)
            reply = _fallback_reply(just_executed["tool"], just_executed["result"])
            audit(
                RamaAudit.Kind.ASSISTANT_MESSAGE,
                {"text": reply, "tools_used": tools_used, "degraded": True},
            )
            return Response(
                {
                    "conversation_id": str(conversation_id),
                    "reply": reply,
                    "provider": provider_name,
                    "model": model,
                    "tools_used": tools_used,
                }
            )
        status_code = getattr(exc, "status_hint", 502) or 502
        # Keep codes in a sensible HTTP range.
        if status_code not in (400, 401, 403, 429, 502, 503):
            status_code = 502
        return Response(
            {
                "detail": str(exc),
                "code": "PROVIDER_ERROR",
                "provider": provider_name,
                "model": model,
            },
            status=status_code,
        )

    _save_pending(landlord, conversation_id, pending_call)
    reply = turn.text.strip() or "I wasn't able to produce an answer — try rephrasing."
    audit(
        RamaAudit.Kind.ASSISTANT_MESSAGE,
        {"text": reply, "tools_used": tools_used},
    )

    return Response(
        {
            "conversation_id": str(conversation_id),
            "reply": reply,
            "provider": provider_name,
            "model": model,
            "tools_used": tools_used,
        }
    )
