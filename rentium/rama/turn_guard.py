"""Non-blocking, cross-worker lease for one visible RAMA conversation turn."""

from __future__ import annotations

import hashlib
from contextlib import contextmanager

from django.core.cache import cache
from django.db import connection


def _lock_number(landlord_id, conversation_id) -> int:
    raw = f"rama:{landlord_id}:{conversation_id}".encode()
    digest = hashlib.blake2b(raw, digest_size=8).digest()
    return int.from_bytes(digest, "big", signed=True)


@contextmanager
def conversation_turn_guard(landlord_id, conversation_id):
    """Yield whether this worker acquired the conversation's turn lease.

    PostgreSQL advisory locks are connection-scoped and work across Django and
    Celery processes. Tests and non-PostgreSQL development fall back to the
    configured cache. The acquisition is non-blocking: a follow-up such as "?"
    receives an immediate busy reply instead of starting another overlapping
    model loop and disappearing behind it.
    """
    if connection.vendor == "postgresql":
        lock_id = _lock_number(landlord_id, conversation_id)
        acquired = False
        try:
            with connection.cursor() as cursor:
                cursor.execute("SELECT pg_try_advisory_lock(%s)", [lock_id])
                acquired = bool(cursor.fetchone()[0])
            yield acquired
        finally:
            if acquired:
                with connection.cursor() as cursor:
                    cursor.execute("SELECT pg_advisory_unlock(%s)", [lock_id])
        return

    key = f"rama:turn:{landlord_id}:{conversation_id}"
    acquired = bool(cache.add(key, "1", timeout=120))
    try:
        yield acquired
    finally:
        if acquired:
            cache.delete(key)
