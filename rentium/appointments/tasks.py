"""
Celery tasks for appointments.

The negotiation loop (request → counter → counter → …) has no natural end if a
party just stops replying. This sweep gives a stale pending viewing a dignified
death: after a quiet stretch it's cancelled so it stops nagging the landlord and
drops out of the public open-request cap, freeing the slot for real leads.
"""

from __future__ import annotations

import logging

from config.celery_app import app

logger = logging.getLogger(__name__)

# How long a viewing may sit in a pending state with no movement before we let
# it go. Generous — people take a few days to reply.
STALE_AFTER_DAYS = 14


@app.task
def expire_stale_viewing_requests() -> dict:
    """Cancel viewings still REQUESTED/AWAITING_REQUESTER after STALE_AFTER_DAYS
    of no update, or whose proposed time has simply passed. Emits the normal
    cancellation event so the requester is told."""
    from django.db.models import Q
    from django.utils import timezone

    from .models import Appointment

    now = timezone.now()
    cutoff = now - timezone.timedelta(days=STALE_AFTER_DAYS)
    pending = (Appointment.Status.REQUESTED, Appointment.Status.AWAITING_REQUESTER)

    stale = Appointment.objects.filter(
        kind=Appointment.Kind.VIEWING, status__in=pending
    ).filter(
        # no movement in a fortnight, OR the requested time is already in the past
        Q(updated_at__lt=cutoff) | Q(starts_at__lt=now)
    )

    cancelled = 0
    for appt in stale:
        try:
            appt.transition_to(Appointment.Status.CANCELLED)
        except Exception:  # noqa: BLE001 — one bad row must not stop the sweep
            logger.exception("could not expire appointment %s", appt.pk)
            continue
        appt.publish_event("appointment.cancelled", cancelled_by="SYSTEM")
        cancelled += 1
    return {"cancelled": cancelled}
