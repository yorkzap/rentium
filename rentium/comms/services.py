"""
Outbound messaging — the landlord-facing half of the channel abstraction.

`send_to_landlord` is deliberately dumb: look up active+verified channels,
filter by category prefs, hand text to the per-channel transport. All
transports fail silently (logged) — a channel outage must never break the
event pipeline or an agent turn.
"""

from __future__ import annotations

import logging

from .models import ChannelAccount

logger = logging.getLogger(__name__)


def _record_landlord_delivery(account, full_text: str, *, category: str, url: str, event=None):
    """Put successful external notifications in the same timeline RAMA reads."""
    if not account.landlord_id:
        return
    from rentium.rama.conversations import record_visible_message
    from rentium.rama.models import RamaMessage

    if account.channel_type == ChannelAccount.ChannelType.TELEGRAM:
        from .tasks import telegram_conversation_id

        conversation_id = telegram_conversation_id(account.address)
        channel = "telegram"
    elif account.channel_type == ChannelAccount.ChannelType.WHATSAPP:
        from .tasks import whatsapp_conversation_id

        conversation_id = whatsapp_conversation_id(account.address)
        channel = "whatsapp"
    else:
        return
    entity_refs = []
    if event is not None:
        if event.lease_id:
            entity_refs.append({"type": "lease", "id": str(event.lease_id)})
        if event.property_id:
            entity_refs.append({"type": "property", "id": str(event.property_id)})
    record_visible_message(
        landlord=account.landlord,
        conversation_id=conversation_id,
        direction=RamaMessage.Direction.OUTBOUND,
        text=full_text,
        channel=channel,
        role="system",
        kind=RamaMessage.Kind.NOTIFICATION,
        external_key=account.address,
        source_event=event,
        entity_refs=entity_refs,
        semantic_payload={
            "source_event": event.event_type if event is not None else "",
            "event_id": str(event.pk) if event is not None else "",
            "category": category,
            "url": url,
        },
        delivery_status=RamaMessage.DeliveryStatus.SENT,
    )


def _dispatch(accounts, text: str, *, category: str, url: str, event=None) -> list[str]:
    """Hand `text` to each account's transport, honouring category prefs.
    Returns the channel_types actually delivered to. All transports fail
    silently (logged) — a channel outage never breaks the caller."""
    sent: list[str] = []
    full_text = f"{text}\n{url}" if url else text
    for account in accounts:
        if not account.wants_category(category):
            continue
        ctype = account.channel_type
        if ctype == ChannelAccount.ChannelType.TELEGRAM:
            from . import telegram

            if telegram.send_message(account.address, full_text):
                sent.append(ctype)
                try:
                    _record_landlord_delivery(
                        account, full_text, category=category, url=url, event=event
                    )
                except Exception:  # noqa: BLE001
                    logger.exception("comms: failed recording visible Telegram delivery")
        elif ctype == ChannelAccount.ChannelType.WHATSAPP:
            from . import whatsapp

            if whatsapp.send_message(account.address, full_text):
                sent.append(ctype)
                try:
                    _record_landlord_delivery(
                        account, full_text, category=category, url=url, event=event
                    )
                except Exception:  # noqa: BLE001
                    logger.exception("comms: failed recording visible WhatsApp delivery")
        else:
            # EMAIL transport lands with its phase; until then a
            # linked-but-unsupported channel is a silent no-op, not a crash.
            logger.info("comms: no transport yet for %s", ctype)
    return sent


def send_to_landlord(
    landlord,
    text: str,
    *,
    category: str = "SYSTEM",
    url: str = "",
    channel_types: list[str] | None = None,
    event=None,
) -> list[str]:
    """Send `text` to every active, verified channel this landlord has linked
    (optionally restricted to `channel_types`), respecting each channel's
    category preferences. Returns the channel_types actually sent to."""
    text = (text or "").strip()
    if not text:
        return []
    accounts = ChannelAccount.objects.filter(
        landlord=landlord, verified=True, is_active=True
    )
    if channel_types:
        accounts = accounts.filter(channel_type__in=channel_types)
    return _dispatch(accounts, text, category=category, url=url, event=event)


def send_to_tenant(
    tenant,
    text: str,
    *,
    category: str = "SYSTEM",
    url: str = "",
    channel_types: list[str] | None = None,
) -> list[str]:
    """Send `text` to a tenant's linked external channels. Same abstraction as
    send_to_landlord — the in-app bell + email are handled separately, so this
    is purely the external hop and a no-op when the tenant has linked nothing."""
    text = (text or "").strip()
    if not text:
        return []
    accounts = ChannelAccount.objects.filter(
        tenant=tenant, verified=True, is_active=True
    )
    if channel_types:
        accounts = accounts.filter(channel_type__in=channel_types)
    return _dispatch(accounts, text, category=category, url=url)
