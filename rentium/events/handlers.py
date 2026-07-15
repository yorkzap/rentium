"""
Event handlers. This is the ONLY place delivery channels live.
Adding mobile push later = add a handler here; zero core-code changes.
The AI co-pilot later = another handler (or a reader of DomainEvent).

Two channels today:
  - Notification rows (the in-app bell) via _notify()
  - email via _send_email() — console backend locally, Anymail in prod

Handlers must stay idempotent-ish and cheap: they run inside the Celery
dispatcher (events.tasks.process_domain_event) and a failure in one never
blocks the others.
"""

import logging

from django.conf import settings
from django.core.mail import send_mail

from .registry import on

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------- helpers
def _notify(user, *, category, title, body="", url="", event=None):
    from .models import Notification

    Notification.objects.create(
        recipient=user,
        event=event,
        category=category,
        title=title,
        body=body,
        url=url,
    )


def _send_email(to, subject, body):
    if not to:
        return
    send_mail(
        subject,
        body,
        getattr(settings, "DEFAULT_FROM_EMAIL", "noreply@rentium.ca"),
        [to],
        fail_silently=True,
    )


def _frontend_url(path: str) -> str:
    base = getattr(settings, "FRONTEND_URL", "http://localhost:3000").rstrip("/")
    return f"{base}{path}"


def _appointment_from(event):
    from rentium.appointments.models import Appointment

    appt_id = (event.payload or {}).get("appointment_id")
    if not appt_id:
        return None
    return (
        Appointment.objects.select_related("property", "landlord__user", "lease")
        .filter(pk=appt_id)
        .first()
    )


def _fmt_when(appt):
    from django.utils import timezone

    local = timezone.localtime(appt.starts_at)
    return local.strftime("%A, %B %-d at %-I:%M %p")


# ----------------------------------------------------------- appointments
@on("appointment.requested")
def on_viewing_requested(event):
    """A prospective tenant asked for a showing: tell the landlord (bell),
    and acknowledge the requester by email with their tracking link."""
    appt = _appointment_from(event)
    if not appt:
        return

    _notify(
        appt.landlord.user,
        category="SYSTEM",
        title=f"Viewing request for {appt.property.name}",
        body=f"{appt.contact_name or 'Someone'} asked to view on {_fmt_when(appt)}.",
        url="/dashboard/inquiries",
        event=event,
    )

    if appt.contact_email:
        status_url = _frontend_url(f"/viewing/status/{appt.public_token}")
        _send_email(
            appt.contact_email,
            f"Your viewing request for {appt.property.name}",
            (
                f"Hi {appt.contact_name},\n\n"
                f"Your request to view {appt.property.name} on {_fmt_when(appt)} "
                "has been sent to the landlord. You'll get another email as soon "
                "as they confirm (or propose a different time).\n\n"
                f"Track your request any time here:\n{status_url}\n\n"
                "— Rentium"
            ),
        )


@on("appointment.scheduled")
def on_appointment_scheduled(event):
    """Confirmed. Email the requester (if this came from the public funnel)
    and notify the tenants whose home it is — this is their entry notice."""
    appt = _appointment_from(event)
    if not appt:
        return

    if appt.contact_email:
        status_url = _frontend_url(f"/viewing/status/{appt.public_token}")
        _send_email(
            appt.contact_email,
            f"Confirmed: viewing at {appt.property.name}",
            (
                f"Hi {appt.contact_name},\n\n"
                f"Good news — the landlord confirmed your viewing of "
                f"{appt.property.name} on {_fmt_when(appt)}.\n\n"
                f"Details and any updates:\n{status_url}\n\n"
                "— Rentium"
            ),
        )

    if appt.lease_id:
        for lt in appt.lease.lease_tenants.select_related("tenant__user"):
            if lt.tenant:
                _notify(
                    lt.tenant.user,
                    category="SYSTEM",
                    title=f"Visit scheduled at {appt.property.name}",
                    body=(
                        f"{appt.get_kind_display()} on {_fmt_when(appt)}. "
                        "This listing is your written notice of entry."
                    ),
                    url="/dashboard/tenancy/calendar",
                    event=event,
                )


@on("appointment.cancelled")
def on_appointment_cancelled(event):
    """Declined or cancelled — the requester deserves to hear it."""
    appt = _appointment_from(event)
    if not appt or not appt.contact_email:
        return
    status_url = _frontend_url(f"/viewing/status/{appt.public_token}")
    _send_email(
        appt.contact_email,
        f"Update on your viewing request for {appt.property.name}",
        (
            f"Hi {appt.contact_name},\n\n"
            f"The viewing of {appt.property.name} you requested for "
            f"{_fmt_when(appt)} won't go ahead at that time. If the place is "
            "still listed, you're welcome to request another slot.\n\n"
            f"Details:\n{status_url}\n\n"
            "— Rentium"
        ),
    )


# ------------------------------------------------------------ maintenance
@on("maintenance.created")
def notify_new_work_order(event):
    from rentium.maintenance.models import WorkOrder

    wo_id = (event.payload or {}).get("work_order_id")
    wo = (
        WorkOrder.objects.select_related("property__landlord__user")
        .filter(pk=wo_id)
        .first()
        if wo_id
        else None
    )
    if not wo:
        return
    _notify(
        wo.property.landlord.user,
        category="MAINTENANCE",
        title=f"New maintenance report: {wo.title}",
        body=f"{wo.property.name} · {wo.get_priority_display()} priority",
        url="/dashboard/maintenance",
        event=event,
    )


@on("maintenance.sla_breached")
def notify_sla_breach(event):
    from rentium.maintenance.models import WorkOrder

    wo_id = (event.payload or {}).get("work_order_id")
    wo = (
        WorkOrder.objects.select_related("property__landlord__user")
        .filter(pk=wo_id)
        .first()
        if wo_id
        else None
    )
    if not wo:
        return
    _notify(
        wo.property.landlord.user,
        category="MAINTENANCE",
        title=f"Response deadline passed: {wo.title}",
        body=f"{wo.property.name} — this emergency repair is past its SLA.",
        url="/dashboard/maintenance",
        event=event,
    )
    _send_email(
        wo.property.landlord.user.email,
        f"[Rentium] Response deadline passed: {wo.title}",
        (
            f"The work order “{wo.title}” at {wo.property.name} is past its "
            "response deadline. Emergency repairs under the RTA must be "
            "addressed promptly.\n\nOpen it: "
            + _frontend_url("/dashboard/maintenance")
        ),
    )


# ----------------------------------------------------------------- ledger
@on("ledger.payment_posted")
def notify_payment_received(event):
    """The landlord recorded it themselves; the TENANT is the one who wants
    the receipt confirmation."""
    from rentium.ledger.models import LedgerEntry

    entry_id = (event.payload or {}).get("entry_id")
    entry = (
        LedgerEntry.objects.select_related("tenant__user", "property")
        .filter(pk=entry_id)
        .first()
        if entry_id
        else None
    )
    if not entry or not entry.tenant:
        return
    _notify(
        entry.tenant.user,
        category="PAYMENT",
        title=f"Payment of ${entry.amount} recorded",
        body=(entry.description or "")[:180],
        url="/dashboard",
        event=event,
    )


@on("ledger.charge_due_soon")
def notify_rent_due_soon(event):
    from rentium.ledger.models import LedgerEntry

    entry_id = (event.payload or {}).get("entry_id")
    entry = (
        LedgerEntry.objects.select_related("tenant__user", "property")
        .filter(pk=entry_id)
        .first()
        if entry_id
        else None
    )
    if not entry or not entry.tenant:
        return
    _notify(
        entry.tenant.user,
        category="PAYMENT",
        title=f"${entry.amount} due {entry.due_date:%b %-d}",
        body=(entry.description or "")[:180],
        url="/dashboard",
        event=event,
    )
