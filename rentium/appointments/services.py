"""
Appointment scheduling logic — the deterministic Python that decides whether a
requested time is inside a landlord's preferred hours, and suggests concrete
slots. Kept out of the views/tools so the public form, the negotiation FSM, and
RAMA's tools all classify a time identically.

Nothing here BLOCKS a booking: a viewing always needs the landlord's explicit
tap. These helpers only label a time and propose good ones.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from django.core.exceptions import ValidationError
from django.db import transaction

from .models import AvailabilityWindow

# time_class values, kept as flat strings so they travel cleanly through JSON
# tool results and a CharField alike.
IN_HOURS = "IN_HOURS"
OUT_OF_HOURS = "OUT_OF_HOURS"
UNSET = "UNSET"  # the landlord hasn't configured any preferred hours yet

DEFAULT_TZ = "America/Vancouver"


@transaction.atomic
def schedule_viewing(
    *,
    landlord,
    property_obj,
    starts_at: datetime,
    contact_name: str = "",
    contact_email: str = "",
    contact_phone: str = "",
    notes: str = "",
):
    """Create and publish a landlord-confirmed property viewing."""
    from .models import Appointment, AppointmentProposal

    if property_obj.landlord_id != landlord.pk:
        raise ValidationError({"property": "That property is outside this portfolio."})
    if starts_at.tzinfo is None:
        starts_at = starts_at.replace(tzinfo=landlord_tz(landlord))
    active_lease = current_active_lease(property_obj)
    appointment = Appointment.objects.create(
        landlord=landlord,
        property=property_obj,
        lease=active_lease,
        kind=Appointment.Kind.VIEWING,
        status=Appointment.Status.SCHEDULED,
        starts_at=starts_at,
        contact_name=(contact_name or "")[:200],
        contact_email=(contact_email or "")[:150],
        contact_phone=(contact_phone or "")[:30],
        notes=(notes or "")[:2000],
        tenant_consent=(
            Appointment.TenantConsent.PENDING
            if active_lease
            else Appointment.TenantConsent.NOT_APPLICABLE
        ),
    )
    appointment.stamp_time_class()
    appointment.save(update_fields=["time_class"])
    appointment.record_proposal(
        by=AppointmentProposal.By.LANDLORD,
        starts_at=starts_at,
        message=notes or "",
    )
    appointment.publish_event("appointment.scheduled")
    if active_lease:
        appointment.publish_event("appointment.tenant_review")
    return appointment


@transaction.atomic
def reschedule_viewing(
    *,
    appointment,
    starts_at: datetime,
    message: str = "",
):
    """Move an existing SCHEDULED (or pending) viewing to a new start time.

    Keeps the same appointment row, public_token, and prospect contact so their
    tracking link still works. Records a proposal for audit and publishes
    appointment.rescheduled so the prospect is emailed the new time.
    """
    from .models import Appointment, AppointmentProposal

    if appointment.kind != Appointment.Kind.VIEWING:
        raise ValidationError({"kind": "Only viewings can be rescheduled this way."})
    if appointment.status == Appointment.Status.CANCELLED:
        raise ValidationError({"status": "A cancelled viewing cannot be rescheduled."})
    if appointment.status == Appointment.Status.COMPLETED:
        raise ValidationError({"status": "A completed viewing cannot be rescheduled."})

    if starts_at.tzinfo is None:
        starts_at = starts_at.replace(tzinfo=landlord_tz(appointment.landlord))

    previous = appointment.starts_at
    if previous == starts_at:
        raise ValidationError({"starts_at": "That is already the scheduled time."})

    appointment.starts_at = starts_at
    appointment.stamp_time_class()
    # Reschedule of an already-confirmed visit stays SCHEDULED (landlord owns
    # the time). Pending requests keep their negotiation state.
    if appointment.status in (
        Appointment.Status.REQUESTED,
        Appointment.Status.AWAITING_REQUESTER,
    ):
        # Landlord setting a firm new time on a pending request is a counter.
        appointment.transition_to(Appointment.Status.AWAITING_REQUESTER)
        appointment.save(update_fields=["starts_at", "time_class", "status", "updated_at"])
        appointment.record_proposal(
            by=AppointmentProposal.By.LANDLORD,
            starts_at=starts_at,
            message=message or "Rescheduled",
        )
        appointment.publish_event(
            "appointment.countered",
            proposed_by="LANDLORD",
            previous_starts_at=previous.isoformat(),
        )
    else:
        appointment.save(update_fields=["starts_at", "time_class", "updated_at"])
        appointment.record_proposal(
            by=AppointmentProposal.By.LANDLORD,
            starts_at=starts_at,
            message=message or "Rescheduled by landlord",
        )
        appointment.publish_event(
            "appointment.rescheduled",
            previous_starts_at=previous.isoformat(),
            rescheduled_by="LANDLORD",
        )
        if appointment.lease_id:
            appointment.publish_event("appointment.tenant_review")
    return appointment


def landlord_tz(landlord) -> ZoneInfo:
    return ZoneInfo(getattr(landlord, "timezone", None) or DEFAULT_TZ)


def preferred_windows(landlord, property=None):
    """
    The effective weekly windows for a property.

    A property's own override rows win outright when any exist; otherwise the
    property inherits the landlord's default (``property IS NULL``) rows.
    Returns a list ordered by weekday, start_time.
    """
    # Recurring weekly windows only — one-off specific_date rows are handled
    # separately (they override just their own date), so they must not leak into
    # the weekly schedule.
    base = AvailabilityWindow.objects.filter(landlord=landlord, specific_date__isnull=True)
    if property is not None:
        overrides = list(base.filter(property=property))
        if overrides:
            return overrides
    return list(base.filter(property__isnull=True))


def _specific_windows(landlord, property, the_date):
    """One-off windows a landlord set for exactly `the_date` (property override
    wins over the default, same as the weekly rule)."""
    base = AvailabilityWindow.objects.filter(landlord=landlord, specific_date=the_date)
    if property is not None:
        overrides = list(base.filter(property=property))
        if overrides:
            return overrides
    return list(base.filter(property__isnull=True))


def classify_time(landlord, property, when: datetime) -> str:
    """
    Label ``when`` as IN_HOURS / OUT_OF_HOURS / UNSET for this landlord+property.

    ``when`` is converted to the landlord's timezone before comparison. A naive
    datetime is assumed to already be in the landlord's local time. UNSET means
    no preferred hours are configured — the caller should say so rather than
    imply the time is bad.
    """
    tz = landlord_tz(landlord)
    local = when.astimezone(tz) if when.tzinfo else when.replace(tzinfo=tz)
    at = local.time()

    # A one-off window for this exact date overrides the weekly schedule.
    specific = _specific_windows(landlord, property, local.date())
    if specific:
        return (
            IN_HOURS
            if any(w.start_time <= at < w.end_time for w in specific)
            else OUT_OF_HOURS
        )

    windows = preferred_windows(landlord, property)
    if not windows:
        return UNSET
    weekday = local.weekday()
    for w in windows:
        if w.weekday == weekday and w.start_time <= at < w.end_time:
            return IN_HOURS
    return OUT_OF_HOURS


def notification_receipt(appt) -> dict:
    """Describe — truthfully and synchronously — who gets told about this
    appointment and on which channels, so RAMA can answer "how were they
    notified?" instead of shrugging that the tool result didn't say.

    Grounded in what's actually wired: the prospect (no account) is reached by
    email + their tracking page; current tenants get an in-app notice + email,
    plus any external channel they've linked. Returns flat, model-friendly data.
    """
    channels: list[str] = []
    recipients: list[dict] = []

    if appt.contact_email:
        channels.append("email")
        recipients.append(
            {
                "who": appt.contact_name or "the viewer",
                "role": "prospective tenant",
                "via": "email + tracking page",
                "address": appt.contact_email,
            }
        )

    if appt.lease_id:
        tenant_channels = {"dashboard", "email"}
        for lt in appt.lease.lease_tenants.select_related("tenant__user"):
            tenant = getattr(lt, "tenant", None)
            user = getattr(tenant, "user", None) if tenant else None
            if not user:
                continue
            via = "in-app + email"
            # Any external channel the tenant has linked (Telegram, later WhatsApp)
            try:
                from rentium.comms.models import ChannelAccount

                if ChannelAccount.objects.filter(
                    tenant=tenant, verified=True, is_active=True
                ).exists():
                    via += " + their linked channel"
                    tenant_channels.add("telegram")
            except Exception:  # noqa: BLE001 — comms may not have tenant support yet
                pass
            recipients.append(
                {
                    "who": user.name or "the current tenant",
                    "role": "current tenant",
                    "via": via,
                }
            )
        channels.extend(sorted(tenant_channels))

    if not channels:
        channels = ["dashboard"]
    # de-dupe, preserve order
    seen: set[str] = set()
    channels = [c for c in channels if not (c in seen or seen.add(c))]
    return {"channels": channels, "recipients": recipients}


def propose_inspection_time(landlord, inspection, when):
    """Open (or re-open) a landlord↔tenant negotiation over WHEN a move-in or
    move-out inspection walkthrough happens. Creates an INSPECTION appointment
    proposing `when`, awaiting the tenant's reply. Unlike a viewing there's no
    third-party occupant — the one party is the incoming/outgoing tenant, who
    responds from their dashboard.
    """
    from .models import Appointment, AppointmentProposal

    lease = inspection.lease
    prop = lease.property
    appt = (
        Appointment.objects.filter(
            inspection=inspection,
            status__in=(
                Appointment.Status.REQUESTED,
                Appointment.Status.AWAITING_REQUESTER,
            ),
        ).first()
    )
    if appt is None:
        appt = Appointment(
            landlord=landlord,
            property=prop,
            lease=lease,
            inspection=inspection,
            kind=Appointment.Kind.INSPECTION,
            status=Appointment.Status.AWAITING_REQUESTER,
        )
    appt.starts_at = when
    appt.status = Appointment.Status.AWAITING_REQUESTER
    appt.save()
    appt.stamp_time_class()
    appt.save(update_fields=["time_class"])
    appt.record_proposal(by=AppointmentProposal.By.LANDLORD, starts_at=when)
    appt.publish_event("appointment.inspection_proposed")
    return appt


def current_active_lease(property):
    """The lease of whoever lives in `property` right now, or None if vacant.

    A showing at an occupied unit is the current tenant's entry notice, so it
    routes to them for consent; a vacant unit has no one to ask.
    """
    from rentium.leases.models import Lease

    return (
        Lease.objects.filter(property=property, status=Lease.LeaseStatus.ACTIVE)
        .order_by("-start_date")
        .first()
    )


def suggest_slots(
    landlord,
    property=None,
    *,
    from_dt: datetime | None = None,
    days: int = 14,
    slot_minutes: int = 60,
    limit: int = 12,
) -> list[datetime]:
    """
    Concrete upcoming datetimes (tz-aware, landlord tz) that fall inside the
    preferred windows — what the public picker offers as one-tap choices. Empty
    when no hours are configured; the picker then falls back to a free time
    input.
    """
    windows = preferred_windows(landlord, property)
    if not windows:
        return []
    tz = landlord_tz(landlord)
    start = (from_dt.astimezone(tz) if from_dt and from_dt.tzinfo else datetime.now(tz))
    by_weekday: dict[int, list[AvailabilityWindow]] = {}
    for w in windows:
        by_weekday.setdefault(w.weekday, []).append(w)

    out: list[datetime] = []
    day = start.date()
    for offset in range(days):
        d = day + timedelta(days=offset)
        for w in by_weekday.get(d.weekday(), []):
            cursor = datetime.combine(d, w.start_time, tzinfo=tz)
            window_end = datetime.combine(d, w.end_time, tzinfo=tz)
            while cursor + timedelta(minutes=slot_minutes) <= window_end:
                if cursor > start:
                    out.append(cursor)
                    if len(out) >= limit:
                        return sorted(out)
                cursor += timedelta(minutes=slot_minutes)
    return sorted(out)
