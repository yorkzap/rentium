"""
RAMA v1: a read-only Q&A agent over the landlord's own portfolio.

The anatomy of one turn: scope is injected (landlord from the session,
never from model output), the model picks tools from the registry, the
registry executes them scoped server-side, and every step lands in
RamaAudit. The model reasons and phrases; the service layer computes.
"""

import json
import uuid

from django.conf import settings
from rest_framework import status as http_status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.exceptions import PermissionDenied
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from .models import RamaAudit
from .providers import PROVIDERS, ProviderError, Turn, get_provider
from .registry import execute, tool_schemas
from .union import state_of_the_union

MAX_TOOL_ROUNDS = 8  # resolve → query → answer fits well inside this
HISTORY_TURNS = 20  # per-conversation memory only, by design
MAX_MESSAGE_CHARS = 4000

SYSTEM_PROMPT = """\
You are RAMA, the assistant inside Rentium, a Canadian property-management \
app. You work for exactly one landlord and can see only their portfolio.

Rules:
- Every number, date, name, and record MUST come from a tool result in this \
conversation. Never invent or estimate portfolio data; if no tool can \
answer, say so plainly.
- You are read-only. You cannot create, edit, send, or delete anything. If \
asked to change something, point to where in the dashboard it's done.
- When resolve_person returns more than one candidate, ask the user which \
one they mean. Never guess between people.
- Amounts are Canadian dollars — write them like $850.
- If a tool result contains an "error" field, tell the user what went wrong \
in plain language.
- Be brief and concrete: a short paragraph or a few bullet points, no \
preamble."""


def _landlord(request):
    profile = getattr(request.user, "landlord_profile", None)
    if profile is None:
        raise PermissionDenied("Landlords only.")
    return profile


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def union_view(request):
    """GET /api/rama/state-of-the-union/

    The portfolio aggregate, straight from the service layer. Not gated on
    RAMA_ENABLED — it's useful without the model.
    """
    return Response(state_of_the_union(_landlord(request)))


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def config_view(request):
    """GET /api/rama/config/ — what the dashboard panel needs to render."""
    _landlord(request)
    provider_cls = PROVIDERS.get(settings.RAMA_PROVIDER)
    configured = bool(
        provider_cls and getattr(settings, provider_cls.api_key_setting, "")
    )
    return Response(
        {
            "enabled": settings.RAMA_ENABLED,
            "configured": configured,
            "provider": settings.RAMA_PROVIDER,
            "model": settings.RAMA_MODEL,
            # Model/provider switching is an operator control, not a
            # per-landlord preference.
            "can_override": bool(request.user.is_staff),
            "providers": sorted(PROVIDERS),
        }
    )


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def chat_view(request):
    """POST /api/rama/chat/ {message, conversation_id?, provider?, model?}

    provider/model overrides are honored for staff only — everyone else
    gets the configured defaults. Whatever ran is stamped on the audit rows
    and echoed in the response.
    """
    if not settings.RAMA_ENABLED:
        return Response(
            {"detail": "RAMA is not enabled on this server."},
            status=http_status.HTTP_403_FORBIDDEN,
        )
    landlord = _landlord(request)

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

    provider_name = settings.RAMA_PROVIDER
    model = settings.RAMA_MODEL
    if request.user.is_staff:
        provider_name = str(request.data.get("provider") or provider_name)
        model = str(request.data.get("model") or model)

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

    # Rebuild this conversation's text turns from the audit trail.
    # Per-conversation memory only — there is deliberately no long-term
    # memory of resolutions across conversations.
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

    schemas = tool_schemas()
    tools_used: list[str] = []
    turn = Turn()
    try:
        for _ in range(MAX_TOOL_ROUNDS):
            turn = provider.complete(
                model=model, system=SYSTEM_PROMPT, messages=messages, tools=schemas
            )
            if not turn.tool_calls:
                break
            messages.append(
                {
                    "role": "assistant",
                    "text": turn.text,
                    "tool_calls": [
                        {"id": c.id, "name": c.name, "arguments": c.arguments}
                        for c in turn.tool_calls
                    ],
                }
            )
            for call in turn.tool_calls:
                result = execute(call.name, call.arguments, landlord=landlord)
                tools_used.append(call.name)
                audit(
                    RamaAudit.Kind.TOOL_CALL,
                    {"tool": call.name, "arguments": call.arguments, "result": result},
                )
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": call.id,
                        "name": call.name,
                        "content": json.dumps(result, default=str),
                    }
                )
        else:
            turn = Turn(
                text=(
                    "That question needed more lookups than I allow in one "
                    "turn — try asking something narrower."
                )
            )
    except ProviderError as exc:
        audit(RamaAudit.Kind.ERROR, {"error": str(exc)})
        return Response(
            {"detail": str(exc)}, status=http_status.HTTP_502_BAD_GATEWAY
        )

    reply = turn.text.strip() or "I wasn't able to produce an answer — try rephrasing."
    audit(RamaAudit.Kind.ASSISTANT_MESSAGE, {"text": reply, "tools_used": tools_used})

    return Response(
        {
            "conversation_id": str(conversation_id),
            "reply": reply,
            "provider": provider_name,
            "model": model,
            "tools_used": tools_used,
        }
    )
