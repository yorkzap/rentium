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


def _hours_hint(appt) -> str:
    """Only worth saying when it's OUTSIDE the landlord's usual hours — that's
    the case that wants a second look."""
    if getattr(appt, "time_class", "") == "OUT_OF_HOURS":
        return " — heads up, that's outside your usual viewing hours"
    return ""


def _ping_landlord(appt, *, title, body, url, event, category="SYSTEM"):
    """Reach the landlord in-app (bell) AND on every external channel they've
    linked (Telegram today, WhatsApp later) — one call, both surfaces. The
    external hop fails silently, so a down bot never breaks the pipeline."""
    _notify(appt.landlord.user, category=category, title=title, body=body, url=url, event=event)
    try:
        from rentium.comms.services import send_to_landlord

        text = f"{title}\n{body}".strip() if body else title
        send_to_landlord(appt.landlord, text, category=category, url=_frontend_url(url))
    except Exception:  # noqa: BLE001 — delivery must never break the handler
        logger.exception("comms fan-out failed for appointment %s", appt.pk)


def _lease_tenants(appt):
    """The account-holding tenants on this appointment's lease (if any)."""
    if not appt.lease_id:
        return []
    return [
        lt.tenant
        for lt in appt.lease.lease_tenants.select_related("tenant__user")
        if lt.tenant and getattr(lt.tenant, "user_id", None)
    ]


def _ping_tenant(tenant, *, title, body, url, event):
    """Reach a current tenant in-app, by email, and on any linked external
    channel. Telegram for tenants lights up once comms grows a tenant subject —
    until then this is bell + email, which is the guaranteed path."""
    _notify(tenant.user, category="SYSTEM", title=title, body=body, url=url, event=event)
    _send_email(tenant.user.email, title, f"{body}\n\n— Rentium")
    try:
        from rentium.comms.services import send_to_tenant

        send_to_tenant(tenant, f"{title}\n{body}".strip(), category="SYSTEM", url=_frontend_url(url))
    except (ImportError, AttributeError):
        pass  # tenant channels not built yet — bell + email already delivered
    except Exception:  # noqa: BLE001
        logger.exception("tenant comms fan-out failed")


# ----------------------------------------------------------- appointments
@on("appointment.requested")
def on_viewing_requested(event):
    """A prospective tenant asked for a showing: tell the landlord (bell),
    and acknowledge the requester by email with their tracking link."""
    appt = _appointment_from(event)
    if not appt:
        return

    _ping_landlord(
        appt,
        title=f"Viewing request for {appt.property.name}",
        body=(
            f"{appt.contact_name or 'Someone'} asked to view on "
            f"{_fmt_when(appt)}{_hours_hint(appt)}."
        ),
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
        # Give them one place to keep talking to the landlord — the prospect
        # thread, reached by its own emailed token (not an open URL).
        chat_line = ""
        try:
            from rentium.messaging.services import (
                chat_url,
                get_or_create_prospect_thread,
            )

            convo = get_or_create_prospect_thread(
                appt.landlord, appt.property, appt.contact_email, appt.contact_name or ""
            )
            chat_line = (
                "Have a question before then? Reply right here and keep the "
                f"conversation going:\n{chat_url(convo)}\n\n"
            )
        except Exception:  # noqa: BLE001 — email must send even if threading hiccups
            chat_line = ""
        _send_email(
            appt.contact_email,
            f"Confirmed: viewing at {appt.property.name}",
            (
                f"Hi {appt.contact_name},\n\n"
                f"Good news — the landlord confirmed your viewing of "
                f"{appt.property.name} on {_fmt_when(appt)}.\n\n"
                f"Details and any updates:\n{status_url}\n\n"
                f"{chat_line}"
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
    """Declined or cancelled. Tell whichever side DIDN'T do the cancelling:
    the requester by email when the landlord declines; the landlord in-app +
    on their channels when the requester withdraws."""
    appt = _appointment_from(event)
    if not appt:
        return
    cancelled_by = (event.payload or {}).get("cancelled_by")

    if cancelled_by == "REQUESTER":
        _ping_landlord(
            appt,
            title=f"Viewing withdrawn — {appt.property.name}",
            body=f"{appt.contact_name or 'The requester'} withdrew their viewing request.",
            url="/dashboard/inquiries",
            event=event,
        )
        return

    if appt.contact_email:
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


@on("appointment.rescheduled")
def on_appointment_rescheduled(event):
    """Landlord moved a confirmed viewing.

    Business rule: every real time change emails the prospect (contact_email)
    with the new when + status link, and emails/notifies any current tenants on
    the lease as an updated notice of entry. Silent reschedules are a bug.
    """
    appt = _appointment_from(event)
    if not appt:
        return
    previous_raw = (event.payload or {}).get("previous_starts_at")
    if previous_raw:
        try:
            from django.utils.dateparse import parse_datetime

            prev_dt = parse_datetime(str(previous_raw))
            previous_label = (
                prev_dt.astimezone().strftime("%A, %B %d at %I:%M %p %Z")
                if prev_dt
                else str(previous_raw)
            )
        except Exception:  # noqa: BLE001
            previous_label = str(previous_raw)
    else:
        previous_label = "the previous time"
    status_url = _frontend_url(f"/viewing/status/{appt.public_token}")

    # Prospect / visitor — always email when we have an address.
    if appt.contact_email:
        _send_email(
            appt.contact_email,
            f"Updated: viewing at {appt.property.name}",
            (
                f"Hi {appt.contact_name or 'there'},\n\n"
                f"Your viewing of {appt.property.name} has been rescheduled.\n\n"
                f"New time: {_fmt_when(appt)}\n"
                f"Previously: {previous_label}\n\n"
                f"Track this visit any time:\n{status_url}\n\n"
                "— Rentium"
            ),
        )

    # Current tenants on the unit — full channel fan-out (in-app + email +
    # linked channels). This is their updated entry notice.
    for tenant in _lease_tenants(appt):
        _ping_tenant(
            tenant,
            title=f"Visit rescheduled at {appt.property.name}",
            body=(
                f"{appt.get_kind_display()} moved to {_fmt_when(appt)} "
                f"(was {previous_label}). This is your updated notice of entry."
            ),
            url="/dashboard/tenancy/calendar",
            event=event,
        )


@on("appointment.countered")
def on_appointment_countered(event):
    """Someone proposed a different time. Tell the OTHER side:
    landlord countered → email the requester with the new time + accept/counter
    link; requester countered → ping the landlord on their channels."""
    appt = _appointment_from(event)
    if not appt:
        return
    who = (event.payload or {}).get("proposed_by")

    if who == "LANDLORD" and appt.contact_email:
        status_url = _frontend_url(f"/viewing/status/{appt.public_token}")
        _send_email(
            appt.contact_email,
            f"A new time proposed for {appt.property.name}",
            (
                f"Hi {appt.contact_name},\n\n"
                f"The landlord proposed a different time for your viewing of "
                f"{appt.property.name}: {_fmt_when(appt)}.\n\n"
                "Accept it, or suggest another time, here:\n"
                f"{status_url}\n\n"
                "— Rentium"
            ),
        )
    elif who == "REQUESTER":
        _ping_landlord(
            appt,
            title=f"New time proposed — {appt.property.name}",
            body=(
                f"{appt.contact_name or 'The requester'} suggested "
                f"{_fmt_when(appt)}{_hours_hint(appt)}. Confirm, counter, or decline."
            ),
            url="/dashboard/inquiries",
            event=event,
        )


@on("appointment.tenant_review")
def on_appointment_tenant_review(event):
    """A showing was requested at an OCCUPIED unit — ask the current tenant(s).
    Advisory: the landlord can proceed regardless, but the tenant deserves the
    notice and a chance to weigh in."""
    appt = _appointment_from(event)
    if not appt:
        return
    for tenant in _lease_tenants(appt):
        _ping_tenant(
            tenant,
            title=f"Viewing requested at your home — {appt.property.name}",
            body=(
                f"Someone asked to view your unit on {_fmt_when(appt)}. This is "
                "your notice of entry. Let your landlord know if that works or "
                "suggest another time — they'll make the final call."
            ),
            url="/dashboard/tenancy/calendar",
            event=event,
        )


@on("appointment.inspection_proposed")
def on_inspection_proposed(event):
    """The landlord proposed an inspection-walkthrough time — ask the tenant to
    accept or suggest another. Landlord↔tenant only; no third party."""
    appt = _appointment_from(event)
    if not appt:
        return
    for tenant in _lease_tenants(appt):
        _ping_tenant(
            tenant,
            title=f"Inspection time proposed — {appt.property.name}",
            body=(
                f"Your landlord proposed {_fmt_when(appt)} for the condition "
                "walk-through. Accept it or suggest another time from your "
                "dashboard."
            ),
            url="/dashboard/tenancy/calendar",
            event=event,
        )


@on("message.created")
def on_message_created_email_prospect(event):
    """When a landlord replies in a PROSPECT thread, the prospect has no in-app
    inbox — email them the reply with a link back to their tokenized chat. (The
    in-app fan-out in events/notify.py handles registered recipients.)"""
    payload = event.payload or {}
    if not payload.get("to_prospect"):
        return
    email = payload.get("prospect_email")
    if not email:
        return
    name = payload.get("prospect_name") or "there"
    landlord = payload.get("landlord_name") or "The landlord"
    chat = payload.get("chat_url") or ""
    _send_email(
        email,
        f"{landlord} replied to your enquiry",
        (
            f"Hi {name},\n\n"
            f"{landlord} sent you a message:\n\n"
            f"\"{payload.get('preview', '')}\"\n\n"
            f"Read it and reply here:\n{chat}\n\n"
            "— Rentium"
        ),
    )


@on("appointment.tenant_responded")
def on_appointment_tenant_responded(event):
    """The tenant weighed in — tell the landlord, flagging an objection loudly
    since proceeding over it is a decision they should make deliberately."""
    appt = _appointment_from(event)
    if not appt:
        return
    consent = (event.payload or {}).get("consent")
    if consent == "OBJECTED":
        title = f"Tenant objected to a viewing — {appt.property.name}"
        body = (
            f"The current tenant raised a concern about the showing on "
            f"{_fmt_when(appt)}."
            + (f' They said: "{appt.tenant_consent_notes}".' if appt.tenant_consent_notes else "")
            + " You can still confirm, but consider re-checking with them first."
        )
    else:
        title = f"Tenant is fine with the viewing — {appt.property.name}"
        body = f"The current tenant is OK with the showing on {_fmt_when(appt)}."
    _ping_landlord(
        appt, title=title, body=body, url="/dashboard/inquiries", event=event
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
