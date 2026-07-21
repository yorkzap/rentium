"""
Telegram transport — the ONLY module that talks to api.telegram.org.

Everything else calls send_message(); tests replace it with a fake. Failures
are logged, never raised: a down bot must not break a turn or an event
handler.
"""

from __future__ import annotations

import logging

import requests
from django.conf import settings

logger = logging.getLogger(__name__)

TIMEOUT_SECONDS = 15
MAX_MESSAGE_CHARS = 4000  # Telegram hard limit is 4096


def _token() -> str:
    return (getattr(settings, "TELEGRAM_BOT_TOKEN", "") or "").strip()


def send_message(chat_id: str, text: str) -> bool:
    """Send `text` to a Telegram chat. Returns True on success."""
    token = _token()
    if not token:
        logger.warning("TELEGRAM_BOT_TOKEN not set; dropping message")
        return False
    text = (text or "").strip()
    if not text:
        return False
    try:
        response = requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat_id, "text": text[:MAX_MESSAGE_CHARS]},
            timeout=TIMEOUT_SECONDS,
        )
        if response.status_code >= 400:
            logger.warning(
                "telegram sendMessage %s: %s", response.status_code,
                response.text[:300],
            )
            return False
        return True
    except requests.RequestException:
        logger.exception("telegram sendMessage failed")
        return False
