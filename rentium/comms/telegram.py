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


def get_file_bytes(file_id: str) -> tuple[bytes, str] | None:
    """Download a Telegram file (e.g. a photo the landlord sent) → (bytes, name).
    Two calls: getFile → file_path, then download from the file endpoint. Returns
    None on any failure (never raises — a bad download must not break the turn)."""
    token = _token()
    if not token or not file_id:
        return None
    try:
        meta = requests.get(
            f"https://api.telegram.org/bot{token}/getFile",
            params={"file_id": file_id},
            timeout=TIMEOUT_SECONDS,
        ).json()
        path = ((meta or {}).get("result") or {}).get("file_path")
        if not path:
            return None
        blob = requests.get(
            f"https://api.telegram.org/file/bot{token}/{path}", timeout=TIMEOUT_SECONDS
        )
        if blob.status_code >= 400 or not blob.content:
            return None
        name = path.rsplit("/", 1)[-1] or "telegram.jpg"
        return blob.content, name
    except requests.RequestException:
        logger.exception("telegram getFile/download failed")
        return None


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


def send_document(chat_id: str, content: bytes, filename: str,
                  caption: str = "") -> bool:
    """Upload a file (e.g. a lease PDF) as a real Telegram attachment. Bytes are
    posted multipart so no public URL / auth token is needed."""
    token = _token()
    if not token or not content:
        return False
    try:
        response = requests.post(
            f"https://api.telegram.org/bot{token}/sendDocument",
            data={"chat_id": chat_id, "caption": caption[:1000]},
            files={"document": (filename or "file", content)},
            timeout=30,
        )
        if response.status_code >= 400:
            logger.warning("telegram sendDocument %s: %s", response.status_code,
                           response.text[:300])
            return False
        return True
    except requests.RequestException:
        logger.exception("telegram sendDocument failed")
        return False


def send_photo(chat_id: str, content: bytes, filename: str = "photo.jpg",
               caption: str = "") -> bool:
    """Send an image as a real Telegram photo (bytes, multipart)."""
    token = _token()
    if not token or not content:
        return False
    try:
        response = requests.post(
            f"https://api.telegram.org/bot{token}/sendPhoto",
            data={"chat_id": chat_id, "caption": caption[:1000]},
            files={"photo": (filename or "photo.jpg", content)},
            timeout=30,
        )
        if response.status_code >= 400:
            logger.warning("telegram sendPhoto %s: %s", response.status_code,
                           response.text[:300])
            return False
        return True
    except requests.RequestException:
        logger.exception("telegram sendPhoto failed")
        return False
