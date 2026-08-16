"""
Celery tasks for comms. The Telegram webhook must ack fast (Telegram retries
on timeout), so the actual agent turn — which can take several provider
round-trips — runs here, off the request/response cycle.
"""

from __future__ import annotations

import logging
import uuid

from django.conf import settings

from config.celery_app import app

# A turn is an interactive model loop that stops itself at
# RAMA_TURN_BUDGET_SECONDS. Under the project-wide 60s soft limit that stop was
# unreachable and Celery killed the turn instead, so the landlord got "something
# broke" in place of the partial answer we were already holding. run_turn clamps
# itself to whatever it is granted, so these two can no longer drift apart.
_TURN_LIMITS = {
    "soft_time_limit": settings.RAMA_TURN_TASK_SOFT_TIME_LIMIT,
    "time_limit": settings.RAMA_TURN_TASK_TIME_LIMIT,
}
# One whole turn per landlord, off the interactive path.
_BATCH_TURN_LIMITS = {
    "soft_time_limit": settings.RAMA_TURN_BATCH_SOFT_TIME_LIMIT,
    "time_limit": settings.RAMA_TURN_BATCH_TIME_LIMIT,
}

logger = logging.getLogger(__name__)

# One Telegram chat = one persistent conversation, so RamaAudit memory and
# pending-plan "yes" confirms work exactly as they do in web chat.
TELEGRAM_CONVERSATION_NAMESPACE = uuid.UUID("6f1c2b1a-2f3a-4a8e-9b0e-9b8f6c9d9a10")
WHATSAPP_CONVERSATION_NAMESPACE = uuid.UUID("7a2d3c2b-3f4b-4b9f-8c1f-0c9e7dae0b21")


def telegram_conversation_id(chat_id: str) -> uuid.UUID:
    return uuid.uuid5(TELEGRAM_CONVERSATION_NAMESPACE, f"tg:{chat_id}")


def whatsapp_conversation_id(wa_id: str) -> uuid.UUID:
    return uuid.uuid5(WHATSAPP_CONVERSATION_NAMESPACE, f"wa:{wa_id}")


@app.task(**_BATCH_TURN_LIMITS)
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
        "DEFAULT: business document. Call catalog_business_document with this "
        "upload_id ONLY first (no scope_query) so OCR runs. Do NOT assume this "
        "is a listing or inspection photo. Do NOT say it 'looks like a property "
        "photo'. Use attach_photo_to_listing ONLY if the caption clearly says "
        "gallery/listing/main photo/for Room X. If they later give a street "
        "address, file against that physical holding — never a room/unit."
    )


def _stage_telegram_document(
    landlord,
    conversation_id,
    file_id: str,
    *,
    preferred_name: str = "",
    mime_type: str = "",
    caption: str = "",
) -> str:
    """Download a Telegram document (PDF etc.) into a sealed attachment batch.

    PDFs cannot use RamaUpload (ImageField). They use the same conversation-owned
    RamaAttachment batch path as the web paperclip so catalog_business_document
    can OCR them via attachment_id.
    """
    from django.core.files.uploadedfile import SimpleUploadedFile

    from rentium.rama.attachment_services import AttachmentError
    from rentium.rama.attachment_services import batch_chat_note
    from rentium.rama.attachment_services import seal_batch
    from rentium.rama.attachment_services import stage_files

    from . import telegram as transport

    got = transport.get_file_bytes(file_id)
    if not got:
        return ""
    data, path_name = got
    name = (preferred_name or path_name or "telegram-document").strip()
    mime = (mime_type or "").strip().casefold()
    # Telegram file paths are often extensionless hashes; recover PDF from magic.
    if data[:4] == b"%PDF" and not name.casefold().endswith(".pdf"):
        name = f"{name}.pdf" if "." not in name else name.rsplit(".", 1)[0] + ".pdf"
        mime = mime or "application/pdf"
    if not mime:
        if name.casefold().endswith(".pdf"):
            mime = "application/pdf"
        elif name.casefold().endswith((".jpg", ".jpeg")):
            mime = "image/jpeg"
        elif name.casefold().endswith(".png"):
            mime = "image/png"
        else:
            mime = "application/octet-stream"
    try:
        upload = SimpleUploadedFile(name[:255], data, content_type=mime[:160])
        batch = stage_files(
            landlord=landlord,
            conversation_id=conversation_id,
            uploads=[upload],
        )
        seal_batch(
            landlord=landlord,
            conversation_id=conversation_id,
            batch_id=str(batch.pk),
        )
        batch.refresh_from_db()
        # What the landlord typed with the file is what routes it: a PDF sent
        # with "get Sarah to sign this" belongs on a lease, not in the expense
        # inbox. Telegram puts that text in the caption.
        return batch_chat_note(batch, caption=caption)
    except AttachmentError:
        logger.exception(
            "telegram document rejected for landlord %s (name=%s mime=%s)",
            landlord.pk,
            name,
            mime,
        )
        return ""
    except Exception:  # noqa: BLE001
        logger.exception(
            "failed staging telegram document for landlord %s", landlord.pk
        )
        return ""


@app.task(bind=True, max_retries=2, **_TURN_LIMITS)
def handle_telegram_message(
    self,
    landlord_id: str,
    chat_id: str,
    text: str,
    photo_file_id: str = "",
    document_file_id: str = "",
    document_name: str = "",
    document_mime: str = "",
    external_message_id: str = "",
    reply_to_external_id: str = "",
) -> None:
    from rentium.rama.service import run_turn
    from rentium.users.models import LandlordProfile

    from . import telegram as transport

    landlord = LandlordProfile.objects.filter(pk=landlord_id).first()
    if landlord is None:
        logger.warning("telegram message for missing landlord %s", landlord_id)
        return

    # Resolve the portfolio to actually operate on. A CO-LANDLORD's own account
    # may be empty (no properties / no working RAMA key); they link Telegram to
    # manage an OWNER's portfolio. acting_landlord() lands on that owner (with the
    # owner's RAMA config/key) — the same smart default the web panel uses — so
    # the co-landlord's bot works without a separate key of their own.
    from rentium.users.access import acting_landlord

    landlord = acting_landlord(landlord.user) or landlord
    conversation_id = telegram_conversation_id(chat_id)

    # A Telegram photo can be either property media OR photographed paperwork.
    # Stage it once; the shared turn engine deterministically routes business
    # records into OCR/holding storage from the landlord's words.
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

    # PDF / file document (Telegram "document" message). Uses attachment batches
    # so OCR via catalog_business_document works the same as web chat.
    if document_file_id:
        note = _stage_telegram_document(
            landlord,
            conversation_id,
            document_file_id,
            preferred_name=document_name,
            mime_type=document_mime,
            # `text` is Telegram's caption by the time it reaches here (the
            # webhook falls back to message.caption when there is no text), and
            # it is what decides whether this is a form to sign or a bill to file.
            caption=text,
        )
        if note:
            default = "The landlord sent a file."
            if (document_name or "").casefold().endswith(".pdf") or (
                document_mime or ""
            ).casefold() == "application/pdf":
                default = "The landlord sent a PDF document."
            text = f"{text}{note}" if text else (default + note)
        else:
            transport.send_message(
                chat_id,
                "I couldn't download that file — try sending the PDF again "
                "(as a document, not a compressed photo if possible).",
            )
            return

    result = run_turn(
        landlord,
        text,
        conversation_id,
        role="general",
        channel="telegram",
        external_key=chat_id,
        external_message_id=external_message_id,
        reply_to_external_id=reply_to_external_id,
    )
    if result.error is not None:
        transport.send_message(
            chat_id, f"Sorry — {result.error.get('detail', 'something went wrong')}"
        )
        return
    transport.send_message(chat_id, _plain(result.reply) or "…")
    _deliver_attachments(landlord, chat_id, result.attachments, transport)


def _plain(text: str) -> str:
    """Strip markdown that Telegram shows as literal noise (**, #, [x](url), etc.)
    — a safety net over the plain-text style directive."""
    import re

    s = text or ""
    s = re.sub(r"\[([^\]]+)\]\((https?://[^)]+)\)", r"\1: \2", s)  # links → "text: url"
    s = re.sub(r"\*\*([^*]+)\*\*", r"\1", s)  # bold
    s = re.sub(r"(?<!\w)[*_`]([^*_`]+)[*_`](?!\w)", r"\1", s)  # italic/code
    s = re.sub(r"^\s{0,3}#{1,6}\s*", "", s, flags=re.MULTILINE)  # headers
    s = re.sub(r"^\s*[-*]\s+", "• ", s, flags=re.MULTILINE)  # bullets
    return s.strip()


def _deliver_attachments(landlord, chat_id, attachments, transport) -> None:
    """Turn a turn's delivery markers into real Telegram files."""
    for att in attachments or []:
        kind = att.get("kind")
        try:
            if kind == "lease_pdf":
                from rentium.leases.models import Lease
                from rentium.leases.pdf import build_lease_pdf

                lease = Lease.objects.filter(pk=att.get("lease_id")).first()
                if lease is not None:
                    transport.send_document(
                        chat_id, build_lease_pdf(lease),
                        att.get("filename") or "lease.pdf",
                        caption=f"Lease {lease.lease_number}",
                    )
            elif kind == "property_photos":
                from rentium.properties.models import Property

                prop = Property.objects.filter(pk=att.get("property_id")).first()
                if prop is not None:
                    for img in prop.property_images.all()[:10]:
                        try:
                            img.image.open("rb")
                            data = img.image.read()
                            img.image.close()
                            transport.send_photo(
                                chat_id, data,
                                caption=att.get("label") or prop.name,
                            )
                        except Exception:  # one bad image mustn't stop the rest
                            logger.exception("telegram photo send failed")
        except Exception:  # a delivery failure must never break the turn
            logger.exception("attachment delivery failed for %s", kind)


@app.task(bind=True, max_retries=2, **_TURN_LIMITS)
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

    # Co-landlord support: operate on the portfolio they actually manage (owner's
    # config/key), matching the web panel + Telegram. See handle_telegram_message.
    from rentium.users.access import acting_landlord

    landlord = acting_landlord(landlord.user) or landlord

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
