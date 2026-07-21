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
import uuid
from dataclasses import dataclass, field

from .models import RamaAudit, RamaPendingPlan
from .plan_runner import (
    PENDING_PLAN_TTL_SECONDS,
    clear_plan,
    load_fresh_plan,
    plan_brief,
    run_plan,
    save_plan,
    save_single,
)
from .plan_runner import plan_to_payload
from .providers import ProviderError, Turn, get_provider
from .registry import execute
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


def _write_label(result: dict) -> str:
    for key in ("property", "lease", "group", "work_order"):
        obj = result.get(key)
        if isinstance(obj, dict):
            return str(obj.get("name") or obj.get("lease_number") or obj.get("id") or "").strip()
    return ""


def _plan_fallback_reply(progress: dict) -> str:
    """Deterministic per-item report for an executed plan — completed work
    must never look like an error, and never needs a model to describe it."""
    parts: list[str] = []
    for it in progress.get("executed") or []:
        parts.append(f"Done: {it.get('target') or it.get('tool')}")
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


def _persist_pending(landlord, conversation_id, pending_spec: dict | None) -> None:
    """End-of-turn invariant: a plan row exists IFF something is outstanding.

    - A preview produced this turn is persisted (replacing any older one).
    - No preview: an unstarted plan from an earlier turn is treated as a
      change of subject and cleared — but a plan PAUSED mid-execution
      (awaiting a step's own confirm) survives an interjected question.
    """
    if pending_spec is None:
        plan = load_fresh_plan(landlord, conversation_id)
        if plan is not None and plan.status != RamaPendingPlan.Status.AWAITING_STEP_CONFIRM:
            clear_plan(landlord, conversation_id)
        return
    if pending_spec["kind"] == "plan":
        save_plan(landlord, conversation_id, pending_spec["payload"])
    else:
        save_single(
            landlord, conversation_id, pending_spec["tool"], pending_spec["arguments"]
        )


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
    system += (
        "\n\n## LIVE PORTFOLIO (authoritative — overrides chat history)\n"
        + json.dumps(safe_context, indent=None, separators=(",", ":"))
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

    # Bare yes/no is fully handled above — answer without any provider
    # round-trip, so a weak model can never "narrate" actions that didn't
    # happen or spin up a fresh plan after a cancellation.
    if deterministic_reply is not None:
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

    # The still-outstanding preview produced THIS turn, if any. Persisted at
    # end of turn so the next "yes" runs deterministically.
    pending_spec: dict | None = None
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
                if (
                    role == "general"
                    and depth == 0
                    and call.name in DELEGATION_TOOL_NAMES
                ):
                    result = _delegate(landlord, call.name, call.arguments)
                else:
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
                    if isinstance(result.get("plan"), dict):
                        # A playbook plan (plan_operation / plan_move_tenant).
                        pending_spec = {"kind": "plan", "payload": result["plan"]}
                    else:
                        pending_spec = {
                            "kind": "single",
                            "tool": call.name,
                            "arguments": call.arguments or {},
                        }
                elif isinstance(result, dict) and (
                    result.get("created")
                    or result.get("updated")
                    or result.get("deleted")
                    or result.get("done")
                ):
                    # A write went through — clear any matching outstanding
                    # preview so we don't ask the landlord to confirm it again.
                    if pending_spec and pending_spec.get("tool") == call.name:
                        pending_spec = None
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
            _persist_pending(landlord, conversation_id, pending_spec)
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

    _persist_pending(landlord, conversation_id, pending_spec)
    outstanding = load_fresh_plan(landlord, conversation_id)
    reply = turn.text.strip() or "I wasn't able to produce an answer — try rephrasing."
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
        pending_plan=plan_brief(outstanding) if outstanding else None,
    )
