"""Invite email delivery tracking (SendGrid / Anymail).

Send stamps ``invite_email_provider_id`` + QUEUED status. Provider webhooks
(and optional Anymail tracking signals) advance status to delivered/bounced/etc.
"""

from __future__ import annotations

import logging

from django.utils import timezone

logger = logging.getLogger(__name__)

# Map provider event names → Appointment.InviteEmailStatus values.
_EVENT_MAP = {
    # SendGrid
    "processed": "QUEUED",
    "delivered": "DELIVERED",
    "open": "OPENED",
    "bounce": "BOUNCED",
    "dropped": "DROPPED",
    "deferred": "DEFERRED",
    "blocked": "DROPPED",
    # Anymail normalizes some of these
    "sent": "QUEUED",
    "rejected": "DROPPED",
    "failed": "FAILED",
    "complained": "DROPPED",
}


def normalize_email_event(event_type: str) -> str | None:
    key = (event_type or "").strip().lower().replace(" ", "_")
    return _EVENT_MAP.get(key)


def apply_invite_email_event(
    *,
    event_type: str,
    provider_id: str = "",
    appointment_id: str = "",
    detail: str = "",
    recipient: str = "",
) -> dict:
    """Update appointment invite_email_* from a provider delivery event.

    Returns a small result dict for logging / webhook responses.
    """
    from rentium.appointments.models import Appointment

    status = normalize_email_event(event_type)
    if not status:
        return {"matched": False, "reason": f"unmapped event: {event_type}"}

    appt = None
    provider_id = (provider_id or "").strip()[:128]
    appointment_id = (appointment_id or "").strip()

    if appointment_id:
        appt = Appointment.objects.filter(pk=appointment_id).first()
    if appt is None and provider_id:
        # SendGrid sometimes returns the id with/without angle brackets.
        candidates = {provider_id, provider_id.strip("<>")}
        if provider_id.startswith("<") is False:
            candidates.add(f"<{provider_id}>")
        appt = (
            Appointment.objects.filter(invite_email_provider_id__in=list(candidates))
            .order_by("-updated_at")
            .first()
        )
        if appt is None:
            # Partial match: some backends store only the left half of the id.
            short = provider_id.strip("<>").split(".")[0]
            if short and len(short) >= 8:
                appt = (
                    Appointment.objects.filter(
                        invite_email_provider_id__icontains=short
                    )
                    .order_by("-updated_at")
                    .first()
                )
    if appt is None and recipient:
        # Last resort: most recent viewing with that contact email that was queued.
        appt = (
            Appointment.objects.filter(
                contact_email__iexact=recipient.strip(),
                kind=Appointment.Kind.VIEWING,
            )
            .exclude(invite_email_status=Appointment.InviteEmailStatus.NONE)
            .order_by("-invite_email_updated_at", "-updated_at")
            .first()
        )

    if appt is None:
        return {"matched": False, "reason": "no appointment", "provider_id": provider_id}

    # Don't regress a stronger status (e.g. OPENED → DELIVERED, DELIVERED → QUEUED).
    rank = {
        "NONE": 0,
        "QUEUED": 1,
        "DEFERRED": 1,
        "DELIVERED": 2,
        "OPENED": 3,
        "BOUNCED": 4,
        "DROPPED": 4,
        "FAILED": 4,
    }
    current = appt.invite_email_status or "NONE"
    if rank.get(status, 0) < rank.get(current, 0) and status not in {
        "BOUNCED",
        "DROPPED",
        "FAILED",
    }:
        return {
            "matched": True,
            "skipped": True,
            "appointment_id": str(appt.pk),
            "status": current,
            "incoming": status,
        }

    appt.invite_email_status = status
    appt.invite_email_updated_at = timezone.now()
    if detail:
        appt.invite_email_detail = str(detail)[:255]
    elif not appt.invite_email_detail:
        appt.invite_email_detail = event_type[:255]
    if provider_id and not appt.invite_email_provider_id:
        appt.invite_email_provider_id = provider_id
    appt.save(
        update_fields=[
            "invite_email_status",
            "invite_email_updated_at",
            "invite_email_detail",
            "invite_email_provider_id",
            "updated_at",
        ]
    )
    return {
        "matched": True,
        "appointment_id": str(appt.pk),
        "status": status,
        "provider_id": appt.invite_email_provider_id,
    }


def process_sendgrid_events(payload) -> dict:
    """Handle a SendGrid Event Webhook batch (list of event dicts)."""
    if isinstance(payload, dict):
        events = payload.get("events") or payload.get("Items") or [payload]
    elif isinstance(payload, list):
        events = payload
    else:
        events = []

    results = []
    for raw in events:
        if not isinstance(raw, dict):
            continue
        event_type = raw.get("event") or raw.get("type") or raw.get("event_type") or ""
        provider_id = (
            raw.get("sg_message_id")
            or raw.get("smtp-id")
            or raw.get("message_id")
            or raw.get("x-message-id")
            or ""
        )
        # Prefer custom arg if we stamped it.
        unique = raw.get("unique_args") or raw.get("unique_arg") or {}
        if isinstance(unique, str):
            unique = {}
        appointment_id = (
            unique.get("appointment_id")
            or raw.get("appointment_id")
            or raw.get("X-Rentium-Appointment-Id")
            or ""
        )
        detail_parts = [
            raw.get("reason") or "",
            raw.get("response") or "",
            raw.get("status") or "",
        ]
        detail = " ".join(p for p in detail_parts if p).strip()[:255]
        results.append(
            apply_invite_email_event(
                event_type=str(event_type),
                provider_id=str(provider_id),
                appointment_id=str(appointment_id),
                detail=detail,
                recipient=str(raw.get("email") or raw.get("recipient") or ""),
            )
        )
    matched = sum(1 for r in results if r.get("matched"))
    return {"processed": len(results), "matched": matched, "results": results}
