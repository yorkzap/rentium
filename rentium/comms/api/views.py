"""
Comms API: authenticated channel management + the ONE public surface, the
Telegram webhook (verified by a secret header, not by any user session).
"""

from __future__ import annotations

import hmac
import logging

from rest_framework import status as http_status
from rest_framework.decorators import (
    api_view,
    permission_classes,
    throttle_classes,
)
from rest_framework.exceptions import PermissionDenied
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.throttling import ScopedRateThrottle

from django.conf import settings

from ..models import ChannelAccount

logger = logging.getLogger(__name__)


class TelegramWebhookThrottle(ScopedRateThrottle):
    scope = "telegram_webhook"


def _subject(request):
    """The channel owner for this request — a landlord or a tenant. Channels
    belong to whichever profile the user has; both may link the bot."""
    profile = getattr(request.user, "landlord_profile", None) or getattr(
        request.user, "tenant_profile", None
    )
    if profile is None:
        raise PermissionDenied("An account profile is required.")
    return profile


def _accounts_for(subject):
    from rentium.users.models import TenantProfile

    key = "tenant" if isinstance(subject, TenantProfile) else "landlord"
    return ChannelAccount.objects.filter(**{key: subject})


# ------------------------------------------------------- authenticated API
@api_view(["GET"])
@permission_classes([IsAuthenticated])
def list_channels(request):
    """GET /api/comms/channels/ — this user's linked + pending channels."""
    subject = _subject(request)
    accounts = _accounts_for(subject).order_by("channel_type", "-created_at")
    return Response(
        {
            "channels": [
                {
                    "id": a.pk,
                    "channel_type": a.channel_type,
                    "display_name": a.display_name,
                    "verified": a.verified,
                    "is_active": a.is_active,
                    "prefs": a.prefs,
                    "link_code": a.link_code if not a.verified else "",
                }
                for a in accounts
            ]
        }
    )


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def create_link_code(request):
    """POST /api/comms/channels/telegram/link-code/ — mint a 10-min code the
    user sends to the bot as `/link CODE` to bind their chat."""
    subject = _subject(request)
    bot_username = (getattr(settings, "TELEGRAM_BOT_USERNAME", "") or "").strip()
    account = ChannelAccount.mint_link_code(
        subject, ChannelAccount.ChannelType.TELEGRAM
    )
    return Response(
        {
            "link_code": account.link_code,
            "expires_at": account.link_code_expires,
            "bot_username": bot_username,
            "instructions": (
                f"Message @{bot_username}: /link {account.link_code}"
                if bot_username
                else f"Send /link {account.link_code} to the Rentium bot."
            ),
        }
    )


@api_view(["PATCH", "DELETE"])
@permission_classes([IsAuthenticated])
def channel_detail(request, channel_id):
    """PATCH /api/comms/channels/<id>/ {prefs, is_active} — DELETE unlinks."""
    subject = _subject(request)
    account = _accounts_for(subject).filter(pk=channel_id).first()
    if account is None:
        return Response({"detail": "Not found."}, status=http_status.HTTP_404_NOT_FOUND)
    if request.method == "DELETE":
        account.delete()
        return Response(status=http_status.HTTP_204_NO_CONTENT)
    data = request.data or {}
    if "prefs" in data and isinstance(data["prefs"], dict):
        account.prefs = data["prefs"]
    if "is_active" in data:
        account.is_active = bool(data["is_active"])
    account.save()
    return Response({"id": account.pk, "prefs": account.prefs, "is_active": account.is_active})


# ------------------------------------------------------------ public webhook
@api_view(["POST"])
@permission_classes([AllowAny])
@throttle_classes([TelegramWebhookThrottle])
def telegram_webhook(request):
    """POST /api/public/comms/telegram/webhook/

    Telegram sends unauthenticated POSTs; the ONLY proof of origin is the
    secret token Telegram echoes back on every request once configured via
    setWebhook(secret_token=...). Compared with hmac.compare_digest to avoid
    a timing side-channel. Any failure here is a 403, not a stack trace —
    this endpoint is on the open internet.
    """
    secret = getattr(settings, "TELEGRAM_WEBHOOK_SECRET", "") or ""
    got = request.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
    if not secret or not hmac.compare_digest(secret, got):
        return Response(status=http_status.HTTP_403_FORBIDDEN)

    message = (request.data or {}).get("message") or {}
    chat = message.get("chat") or {}
    chat_id = str(chat.get("id") or "").strip()
    text = str(message.get("text") or "").strip()
    # A photo message has no `text` — it has a `photo` array (sizes, largest
    # last) and an optional `caption`. Capture the biggest size's file_id so the
    # landlord can "send RAMA a photo" from Telegram, mirroring the web paperclip.
    photos = message.get("photo") or []
    photo_file_id = str(photos[-1].get("file_id")) if photos else ""
    # PDFs and other files arrive as `document` (not photo). Without this branch
    # the bot acks the webhook, stages nothing, and the model falsely claims it
    # cannot see files.
    document = message.get("document") or {}
    external_message_id = str(message.get("message_id") or "").strip()
    reply_to_external_id = str(
        (message.get("reply_to_message") or {}).get("message_id") or ""
    ).strip()
    document_file_id = str(document.get("file_id") or "").strip()
    document_name = str(document.get("file_name") or "").strip()
    document_mime = str(document.get("mime_type") or "").strip()
    if not text and message.get("caption"):
        text = str(message.get("caption") or "").strip()
    if not chat_id or (
        not text and not photo_file_id and not document_file_id
    ):
        return Response({"ok": True})  # nothing actionable; ack anyway

    from .. import telegram as transport

    if text.startswith("/link"):
        code = text[len("/link") :].strip()
        account = ChannelAccount.redeem_link_code(
            code,
            channel_type=ChannelAccount.ChannelType.TELEGRAM,
            address=chat_id,
            display_name=(chat.get("username") or chat.get("first_name") or ""),
        )
        transport.send_message(
            chat_id,
            "Linked! I'll message you here from now on."
            if account
            else "That code is invalid or expired — mint a new one from Settings → Channels.",
        )
        return Response({"ok": True})

    account = ChannelAccount.objects.filter(
        channel_type=ChannelAccount.ChannelType.TELEGRAM,
        address=chat_id,
        verified=True,
        is_active=True,
    ).first()
    if account is None:
        transport.send_message(
            chat_id,
            "This chat isn't linked to a Rentium account yet. Open "
            "Settings → Channels in the app to get a link code, then send "
            "/link CODE here.",
        )
        return Response({"ok": True})

    # SECURITY: RAMA is a landlord tool. A linked TENANT chat must never drive a
    # RAMA turn (that would expose landlord-scoped tools). Tenants get a canned
    # pointer back to the app; their real actions happen there or by email.
    if account.is_tenant:
        transport.send_message(
            chat_id,
            "Thanks! You'll get viewing notices and reminders here. To reply to "
            "a request or manage your tenancy, open the Rentium app.",
        )
        return Response({"ok": True})

    from ..tasks import handle_telegram_message

    task_kwargs = {
        "photo_file_id": photo_file_id,
        "document_file_id": document_file_id,
        "document_name": document_name,
        "document_mime": document_mime,
    }
    # Preserve the long-standing Celery call shape for old clients/tests while
    # carrying Telegram reply identity whenever Telegram actually supplied it.
    if external_message_id:
        task_kwargs["external_message_id"] = external_message_id
    if reply_to_external_id:
        task_kwargs["reply_to_external_id"] = reply_to_external_id
    handle_telegram_message.delay(
        str(account.landlord_id), chat_id, text, **task_kwargs
    )
    return Response({"ok": True})


# ------------------------------------------------- WhatsApp webhook (pluggable)
class WhatsappWebhookThrottle(ScopedRateThrottle):
    scope = "whatsapp_webhook"


def _wa_link_or_route(wa_id: str, text: str):
    """Shared inbound handling: bind a /link code, else route a message from a
    verified WhatsApp ChannelAccount. Mirrors the Telegram flow so behaviour is
    identical across channels."""
    from .. import whatsapp as transport

    if text.startswith("/link"):
        code = text[len("/link"):].strip()
        account = ChannelAccount.redeem_link_code(
            code,
            channel_type=ChannelAccount.ChannelType.WHATSAPP,
            address=wa_id,
        )
        transport.send_message(
            wa_id,
            "Linked! I'll message you here from now on."
            if account
            else "That code is invalid or expired — mint a new one from Settings → Channels.",
        )
        return

    account = ChannelAccount.objects.filter(
        channel_type=ChannelAccount.ChannelType.WHATSAPP,
        address=wa_id,
        verified=True,
        is_active=True,
    ).first()
    if account is None:
        transport.send_message(
            wa_id,
            "This number isn't linked to a Rentium account yet. Open "
            "Settings → Channels in the app to get a link code, then send "
            "/link CODE here.",
        )
        return

    # SECURITY: same rule as Telegram — a tenant chat must never drive a RAMA
    # turn (landlord tools). Tenants get a canned pointer back to the app.
    if account.is_tenant:
        transport.send_message(
            wa_id,
            "Thanks! You'll get viewing notices and reminders here. To reply to "
            "a request or manage your tenancy, open the Rentium app.",
        )
        return

    from ..tasks import handle_whatsapp_message

    handle_whatsapp_message.delay(str(account.landlord_id), wa_id, text)


@api_view(["GET", "POST"])
@permission_classes([AllowAny])
@throttle_classes([WhatsappWebhookThrottle])
def whatsapp_webhook(request):
    """POST/GET /api/public/comms/whatsapp/webhook/ — Meta WhatsApp Cloud API.

    GET is Meta's subscription handshake (echo hub.challenge when the verify
    token matches). POST carries inbound messages; when WHATSAPP_APP_SECRET is
    set we verify the X-Hub-Signature-256 HMAC first. Unconfigured → 403, never
    a stack trace: this endpoint is on the open internet.
    """
    # --- subscription verification handshake ---
    if request.method == "GET":
        verify_token = getattr(settings, "WHATSAPP_VERIFY_TOKEN", "") or ""
        mode = request.query_params.get("hub.mode")
        got = request.query_params.get("hub.verify_token", "")
        challenge = request.query_params.get("hub.challenge", "")
        if mode == "subscribe" and verify_token and hmac.compare_digest(verify_token, got):
            return Response(int(challenge) if challenge.isdigit() else challenge)
        return Response(status=http_status.HTTP_403_FORBIDDEN)

    # --- inbound messages ---
    app_secret = getattr(settings, "WHATSAPP_APP_SECRET", "") or ""
    if app_secret:
        import hashlib

        signature = request.headers.get("X-Hub-Signature-256", "")
        expected = "sha256=" + hmac.new(
            app_secret.encode(), request.body, hashlib.sha256
        ).hexdigest()
        if not hmac.compare_digest(expected, signature):
            return Response(status=http_status.HTTP_403_FORBIDDEN)

    data = request.data or {}
    try:
        for entry in data.get("entry", []) or []:
            for change in entry.get("changes", []) or []:
                value = change.get("value", {}) or {}
                for msg in value.get("messages", []) or []:
                    wa_id = str(msg.get("from") or "").strip()
                    text = str((msg.get("text") or {}).get("body") or "").strip()
                    if wa_id and text:
                        _wa_link_or_route(wa_id, text)
    except Exception:  # noqa: BLE001 — ack anyway; never leak a trace to the internet
        logger.exception("whatsapp webhook parse failed")

    return Response({"ok": True})
