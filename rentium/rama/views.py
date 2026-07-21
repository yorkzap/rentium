"""
RAMA HTTP surface — thin adapters over the turn engine in service.py.

Preferences (enabled / provider / model / optional BYOK api_key) are
per-landlord. Chat memory is rebuilt from this landlord's RamaAudit rows.
The engine itself (roles, confirm state machine, grounding) lives in
service.run_turn so Telegram webhooks, scheduled analyses, and delegation
call the exact same code path.
"""

import uuid

from rest_framework import status as http_status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.exceptions import PermissionDenied
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from .models import RamaPreferences
from .providers import PROVIDERS
from .runtime import (
    MODEL_CATALOG,
    get_landlord_config,
    get_role_config,
    platform_api_key,
)
from .service import MAX_MESSAGE_CHARS, PENDING_ACTION_TTL_SECONDS, run_turn
from .union import state_of_the_union

__all__ = [
    "chat_view",
    "general_chat_view",
    "constitution_view",
    "insights_view",
    "insight_detail_view",
    "holdings_view",
    "bank_balances_view",
    "config_view",
    "settings_view",
    "union_view",
    "PENDING_ACTION_TTL_SECONDS",  # re-exported for tests/back-compat
]


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
        # Provider is always selectable; "ready" means it can actually be
        # called right now: the landlord's BYOK key (which only applies to
        # their chosen provider) or a platform key. A provider with neither
        # must NOT read as ready just because it is currently selected.
        "platform_ready": {
            name: bool(platform_api_key(name))
            or (name == prefs.provider and bool((prefs.api_key or "").strip()))
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


def _validated_chat_input(request):
    """(message, conversation_id, error_response) shared by chat endpoints."""
    message = str(request.data.get("message") or "").strip()
    if not message:
        return None, None, Response(
            {"detail": "message is required."},
            status=http_status.HTTP_400_BAD_REQUEST,
        )
    if len(message) > MAX_MESSAGE_CHARS:
        return None, None, Response(
            {"detail": f"message is limited to {MAX_MESSAGE_CHARS} characters."},
            status=http_status.HTTP_400_BAD_REQUEST,
        )
    raw_conversation = request.data.get("conversation_id")
    try:
        conversation_id = (
            uuid.UUID(str(raw_conversation)) if raw_conversation else None
        )
    except ValueError:
        return None, None, Response(
            {"detail": "conversation_id must be a UUID."},
            status=http_status.HTTP_400_BAD_REQUEST,
        )
    return message, conversation_id, None


def _chat_response(result) -> Response:
    if result.error is not None:
        status_code = result.error.get("status_hint") or 502
        return Response(
            {
                "detail": result.error["detail"],
                "code": result.error.get("code", "PROVIDER_ERROR"),
                "provider": result.provider,
                "model": result.model,
            },
            status=status_code,
        )
    return Response(
        {
            "conversation_id": str(result.conversation_id),
            "reply": result.reply,
            "provider": result.provider,
            "model": result.model,
            "tools_used": result.tools_used,
            "pending_plan": result.pending_plan,
        }
    )


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def chat_view(request):
    """POST /api/rama/chat/ {message, conversation_id?} — the Corporal."""
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

    message, conversation_id, err = _validated_chat_input(request)
    if err is not None:
        return err

    result = run_turn(
        landlord, message, conversation_id, role="corporal", channel="web"
    )
    return _chat_response(result)


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def insights_view(request):
    """GET /api/rama/insights/?status=OPEN — Sergeant findings the FSA has
    analyzed, most recent first."""
    from .models import RamaInsight

    landlord = _landlord(request)
    qs = RamaInsight.objects.filter(landlord=landlord)
    status_filter = request.query_params.get("status")
    if status_filter:
        qs = qs.filter(status=status_filter.upper())
    return Response(
        {
            "insights": [
                {
                    "id": i.pk,
                    "kind": i.kind,
                    "severity": i.severity,
                    "facts": i.facts,
                    "analysis": i.analysis,
                    "status": i.status,
                    "created_at": i.created_at,
                }
                for i in qs[:100]
            ]
        }
    )


@api_view(["PATCH"])
@permission_classes([IsAuthenticated])
def insight_detail_view(request, insight_id):
    """PATCH /api/rama/insights/<id>/ {status: ACKED|ACTIONED|DISMISSED}"""
    from .models import RamaInsight

    landlord = _landlord(request)
    insight = RamaInsight.objects.filter(pk=insight_id, landlord=landlord).first()
    if insight is None:
        return Response({"detail": "Not found."}, status=http_status.HTTP_404_NOT_FOUND)
    new_status = str(request.data.get("status") or "").strip().upper()
    valid = {c for c, _ in RamaInsight.Status.choices}
    if new_status not in valid:
        return Response(
            {"detail": f"status must be one of {sorted(valid)}."},
            status=http_status.HTTP_400_BAD_REQUEST,
        )
    insight.status = new_status
    insight.save(update_fields=["status", "updated_at"])
    return Response({"id": insight.pk, "status": insight.status})


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def holdings_view(request):
    """GET /api/rama/holdings/ — houses/buildings and their listings (what a
    bank-balance policy attaches to)."""
    from .domain_crud import list_holdings

    return Response(list_holdings(_landlord(request)))


@api_view(["GET", "POST"])
@permission_classes([IsAuthenticated])
def bank_balances_view(request):
    """GET /api/rama/bank-balances/ — reported balances (+ staleness, drift).
    POST {holding_id?, label?, balance, as_of?} — a landlord-direct entry
    (no chat confirm needed for their own UI edit; chat/General-origin
    writes go through the guarded update_bank_balance tool instead)."""
    from .finance import balance_payload, list_bank_balances

    landlord = _landlord(request)
    if request.method == "GET":
        return Response(list_bank_balances(landlord))

    from datetime import date as _date
    from decimal import Decimal, InvalidOperation

    from rentium.ledger.models import PropertyBankBalance
    from rentium.properties.models import PropertyHolding

    data = request.data or {}
    holding = None
    holding_id = data.get("holding_id")
    if holding_id:
        holding = PropertyHolding.objects.filter(pk=holding_id, landlord=landlord).first()
        if holding is None:
            return Response(
                {"detail": "No such holding."}, status=http_status.HTTP_400_BAD_REQUEST
            )
    try:
        balance = Decimal(str(data.get("balance")))
    except (InvalidOperation, TypeError):
        return Response(
            {"detail": "balance is required and must be a number."},
            status=http_status.HTTP_400_BAD_REQUEST,
        )
    as_of_raw = str(data.get("as_of") or "").strip()
    try:
        as_of = _date.fromisoformat(as_of_raw) if as_of_raw else _date.today()
    except ValueError:
        return Response(
            {"detail": "as_of must be YYYY-MM-DD."}, status=http_status.HTTP_400_BAD_REQUEST
        )
    row, _created = PropertyBankBalance.objects.update_or_create(
        landlord=landlord, holding=holding,
        defaults={
            "label": str(data.get("label") or "Operating")[:100],
            "balance": balance,
            "as_of": as_of,
            "updated_via": PropertyBankBalance.Source.UI,
        },
    )
    return Response(balance_payload(row))


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def general_chat_view(request):
    """POST /api/rama/general/chat/ — the General (chief of staff).

    Also the seam an external personal-assistant client uses later.
    """
    landlord = _landlord(request)
    cfg = get_landlord_config(landlord)
    if not cfg.enabled:
        return Response(
            {"detail": "RAMA is turned off for your account."},
            status=http_status.HTTP_403_FORBIDDEN,
        )
    role_cfg = get_role_config(landlord, "general")
    if not role_cfg.api_key:
        return Response(
            {
                "detail": (
                    "No API key available for the General's provider "
                    f"({role_cfg.provider}). Set one under Settings → RAMA."
                )
            },
            status=http_status.HTTP_503_SERVICE_UNAVAILABLE,
        )

    message, conversation_id, err = _validated_chat_input(request)
    if err is not None:
        return err

    result = run_turn(
        landlord, message, conversation_id, role="general", channel="web"
    )
    return _chat_response(result)


@api_view(["GET", "POST"])
@permission_classes([IsAuthenticated])
def constitution_view(request):
    """GET/POST /api/rama/constitution/ — the landlord's written policy.

    GET returns active sections + rules. POST amends one section (creates a
    new append-only version, origin LANDLORD — the landlord's own edits don't
    need the chat confirm gate; General-origin amendments go through the
    guarded amend_constitution tool instead).
    """
    from .constitution import amend, parse_rule_changes, section_payload
    from .models import RamaConstitutionSection

    landlord = _landlord(request)
    if request.method == "GET":
        return Response(section_payload(landlord))

    data = request.data or {}
    key = str(data.get("key") or "").strip().lower()
    if not key:
        return Response(
            {"detail": "key is required."}, status=http_status.HTTP_400_BAD_REQUEST
        )
    raw_changes = data.get("rule_changes")
    if isinstance(raw_changes, (list, dict)):
        import json as _json

        raw_changes = _json.dumps(raw_changes)
    changes, err_msg = parse_rule_changes(str(raw_changes or ""))
    if err_msg:
        return Response(
            {"detail": err_msg}, status=http_status.HTTP_400_BAD_REQUEST
        )
    result = amend(
        landlord,
        key=key,
        title=str(data.get("title") or ""),
        body_md=str(data.get("body_md") or ""),
        rule_changes=changes,
        origin=RamaConstitutionSection.Origin.LANDLORD,
    )
    return Response({**result, **section_payload(landlord)})
