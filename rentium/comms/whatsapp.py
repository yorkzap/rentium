"""
WhatsApp transport — the ONLY module that talks to a WhatsApp provider.

Deliberately pluggable: the provider HTTP call is isolated in `_provider_send`,
so switching Meta Cloud API ↔ Twilio ↔ 360dialog is a one-function change and
nothing else in comms or RAMA moves. The default targets Meta's WhatsApp Cloud
API.

Contract mirrors telegram.py exactly: `send_message(address, text) -> bool`,
failures are logged and swallowed (never raised), and an unconfigured provider
is a safe no-op — so a landlord who has "linked" WhatsApp before you've wired a
provider simply doesn't receive the message, rather than breaking a turn or an
event handler.
"""

from __future__ import annotations

import logging

import requests
from django.conf import settings

logger = logging.getLogger(__name__)

TIMEOUT_SECONDS = 15
MAX_MESSAGE_CHARS = 4000  # WhatsApp text body limit is 4096


def _provider_send(address: str, text: str) -> bool:
    """Provider-specific delivery. DEFAULT: Meta WhatsApp Cloud API.

    Configure via settings/env:
      WHATSAPP_TOKEN            — a permanent/system-user access token
      WHATSAPP_PHONE_NUMBER_ID  — the sending number's id

    To move to Twilio/360dialog, replace the body of this function — the
    send_message contract above it does not change.
    """
    token = (getattr(settings, "WHATSAPP_TOKEN", "") or "").strip()
    phone_number_id = (
        getattr(settings, "WHATSAPP_PHONE_NUMBER_ID", "") or ""
    ).strip()
    if not token or not phone_number_id:
        logger.warning("WhatsApp not configured; dropping message")
        return False

    api_version = getattr(settings, "WHATSAPP_API_VERSION", "v21.0")
    try:
        response = requests.post(
            f"https://graph.facebook.com/{api_version}/{phone_number_id}/messages",
            headers={"Authorization": f"Bearer {token}"},
            json={
                "messaging_product": "whatsapp",
                "to": address,
                "type": "text",
                "text": {"body": text[:MAX_MESSAGE_CHARS]},
            },
            timeout=TIMEOUT_SECONDS,
        )
        if response.status_code >= 400:
            logger.warning(
                "whatsapp send %s: %s", response.status_code, response.text[:300]
            )
            return False
        return True
    except requests.RequestException:
        logger.exception("whatsapp send failed")
        return False


def send_message(address: str, text: str) -> bool:
    """Send `text` to a WhatsApp address (E.164, e.g. 15551234567). Returns
    True on success. Never raises."""
    address = (address or "").strip()
    text = (text or "").strip()
    if not address or not text:
        return False
    try:
        return _provider_send(address, text)
    except Exception:  # noqa: BLE001 — a down provider must never break a turn
        logger.exception("whatsapp send_message failed")
        return False
