"""
Celery tasks for comms. The Telegram webhook must ack fast (Telegram retries
on timeout), so the actual agent turn — which can take several provider
round-trips — runs here, off the request/response cycle.
"""

from __future__ import annotations

import logging
import uuid

from config.celery_app import app

logger = logging.getLogger(__name__)

# One Telegram chat = one persistent conversation, so RamaAudit memory and
# pending-plan "yes" confirms work exactly as they do in web chat.
TELEGRAM_CONVERSATION_NAMESPACE = uuid.UUID("6f1c2b1a-2f3a-4a8e-9b0e-9b8f6c9d9a10")
WHATSAPP_CONVERSATION_NAMESPACE = uuid.UUID("7a2d3c2b-3f4b-4b9f-8c1f-0c9e7dae0b21")


def telegram_conversation_id(chat_id: str) -> uuid.UUID:
    return uuid.uuid5(TELEGRAM_CONVERSATION_NAMESPACE, f"tg:{chat_id}")


def whatsapp_conversation_id(wa_id: str) -> uuid.UUID:
    return uuid.uuid5(WHATSAPP_CONVERSATION_NAMESPACE, f"wa:{wa_id}")


@app.task
def send_morning_briefings() -> dict:
    """Beat entry point: one deterministic digest per landlord, sent to
    every channel that opted in (prefs.briefing = true). $0 LLM by default
    — rama.briefing.build_briefing_text is pure Python."""
    from rentium.rama.briefing import build_briefing_text

    from .models import ChannelAccount

    accounts = [
        a
        for a in ChannelAccount.objects.filter(
            verified=True, is_active=True, landlord__isnull=False
        ).select_related("landlord")
        if (a.prefs or {}).get("briefing")
    ]
    text_cache: dict = {}
    sent = 0
    for account in accounts:
        if account.landlord_id not in text_cache:
            text_cache[account.landlord_id] = build_briefing_text(account.landlord)
        text = text_cache[account.landlord_id]
        if account.channel_type == ChannelAccount.ChannelType.TELEGRAM:
            from . import telegram as transport

            if transport.send_message(account.address, text):
                sent += 1
    return {"briefings_sent": sent, "landlords": len(text_cache)}


def _stage_telegram_photo(landlord, file_id: str) -> str:
    """Download a Telegram photo → stage a landlord-scoped RamaUpload → return the
    attachment note RAMA reads (same shape as the web paperclip). '' on failure."""
    from django.core.files.base import ContentFile

    from rentium.rama.models import RamaUpload

    from . import telegram as transport

    got = transport.get_file_bytes(file_id)
    if not got:
        return ""
    data, name = got
    upload = RamaUpload(landlord=landlord)
    try:
        upload.image.save(name, ContentFile(data), save=True)
    except Exception:  # noqa: BLE001
        logger.exception("failed staging telegram photo for landlord %s", landlord.pk)
        return ""
    return (
        f"\n\n[The landlord attached a photo, upload_id={upload.pk}]\n"
        "To put it on a listing, use attach_photo_to_listing with that upload_id "
        "(hand it to the ops agent if you don't have that tool yourself). Ask "
        "which listing if they didn't say."
    )


@app.task(bind=True, max_retries=2)
def handle_telegram_message(
    self, landlord_id: str, chat_id: str, text: str, photo_file_id: str = ""
) -> None:
    from rentium.rama.service import run_turn
    from rentium.users.models import LandlordProfile

    from . import telegram as transport

    landlord = LandlordProfile.objects.filter(pk=landlord_id).first()
    if landlord is None:
        logger.warning("telegram message for missing landlord %s", landlord_id)
        return

    # A photo the landlord sent to the bot: download it, stage it as a RamaUpload
    # (landlord-scoped), and tell RAMA it's attached — the same note the web
    # paperclip adds — so it can attach_photo_to_listing it.
    if photo_file_id:
        note = _stage_telegram_photo(landlord, photo_file_id)
        if note:
            text = f"{text}{note}" if text else (
                "The landlord sent a photo." + note
            )
        else:
            transport.send_message(
                chat_id, "I couldn't download that photo — try sending it again."
            )
            return

    result = run_turn(
        landlord,
        text,
        telegram_conversation_id(chat_id),
        role="general",
        channel="telegram",
    )
    if result.error is not None:
        transport.send_message(
            chat_id, f"Sorry — {result.error.get('detail', 'something went wrong')}"
        )
        return
    transport.send_message(chat_id, result.reply or "…")


@app.task(bind=True, max_retries=2)
def handle_whatsapp_message(self, landlord_id: str, wa_id: str, text: str) -> None:
    """Same seam as handle_telegram_message, for WhatsApp. Only LANDLORD chats
    reach here (the webhook sends a tenant a canned reply), so RAMA's
    landlord-scoped tools are never exposed to a tenant."""
    from rentium.rama.service import run_turn
    from rentium.users.models import LandlordProfile

    from . import whatsapp as transport

    landlord = LandlordProfile.objects.filter(pk=landlord_id).first()
    if landlord is None:
        logger.warning("whatsapp message for missing landlord %s", landlord_id)
        return

    result = run_turn(
        landlord,
        text,
        whatsapp_conversation_id(wa_id),
        role="general",
        channel="whatsapp",
    )
    if result.error is not None:
        transport.send_message(
            wa_id, f"Sorry — {result.error.get('detail', 'something went wrong')}"
        )
        return
    transport.send_message(wa_id, result.reply or "…")
