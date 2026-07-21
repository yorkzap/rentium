"""
The public showing funnel. A prospective tenant — NO account — opens a public
booking link, sees a privacy-safe teaser, and submits a requested time. That
creates a REQUESTED appointment the landlord confirms or declines.

FIXED (was a real leak): this used to serve ANY property by primary key —
opted-in or not, individually hidden or not, occupied or not. Anyone could
enumerate ids and pull back the name/city/photo of every property in the
database, including a landlord who had never consented to a public presence.
It now goes through Property.objects.public() — the single visibility rule
that governs the whole public site — so it obeys the opt-in like everything
else. It also now resolves by public_slug, so ids aren't guessable.

Privacy: the teaser exposes name, neighbourhood/city, category and photo only
— never the street address, unit numbers, or anything about current tenants.

Abuse: throttled anonymously (scope "viewing_request"), plus an open-request
cap per property.
"""

from django.utils import timezone
from django.utils.dateparse import parse_datetime
from rest_framework.decorators import api_view
from rest_framework.decorators import permission_classes
from rest_framework.decorators import throttle_classes
from rest_framework.exceptions import NotFound
from rest_framework.exceptions import ValidationError
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.throttling import ScopedRateThrottle

from rentium.properties.models import Property

from ..models import Appointment, AppointmentProposal
from ..services import current_active_lease, suggest_slots

MAX_OPEN_REQUESTS_PER_PROPERTY = 25  # crude spam guard


class ViewingRequestThrottle(ScopedRateThrottle):
    scope = "viewing_request"


def _public_property_or_404(identifier):
    """
    Resolve by public_slug first (what we hand out now); fall back to pk so
    any old /viewing/<id> links a landlord already shared keep working. Either
    way it goes through .public(), so consent is enforced on both paths.
    """
    qs = Property.objects.public()
    prop = qs.filter(public_slug=str(identifier)).first()
    if prop:
        return prop
    if str(identifier).isdigit():
        prop = qs.filter(pk=int(identifier)).first()
        if prop:
            return prop
    raise NotFound("This place isn't available.")


@api_view(["GET"])
@permission_classes([AllowAny])
@throttle_classes([ViewingRequestThrottle])
def public_property(request, property_id):
    prop = _public_property_or_404(property_id)

    image = None
    if prop.primary_image:
        try:
            image = request.build_absolute_uri(prop.primary_image.url)
        except Exception:
            image = None

    return Response(
        {
            "id": prop.id,
            "slug": prop.public_slug,
            "name": prop.name,
            # Coarse location only. Never prop.address.
            "location": prop.public_location,
            "city": prop.city,
            "province": prop.province_code,
            "category": prop.get_property_category_display(),
            "room_type": prop.get_room_type_display() if prop.room_type else None,
            "type_label": prop.public_type_label,
            "asking_rent": str(prop.asking_rent) if prop.asking_rent else None,
            "is_furnished": prop.is_furnished,
            # By construction: anything reachable here is AVAILABLE.
            "available": True,
            "image": image,
        }
    )


@api_view(["GET"])
@permission_classes([AllowAny])
@throttle_classes([ViewingRequestThrottle])
def public_viewing_slots(request, property_id):
    """Suggested viewing times inside the landlord's preferred hours, to steer
    the requester's picker toward slots that are likely to be accepted. Empty
    ``slots`` (hours_set false) means the landlord hasn't set hours — the picker
    then just offers a free time input. Never blocks any time."""
    prop = _public_property_or_404(property_id)
    slots = suggest_slots(prop.landlord, prop, limit=12)
    return Response(
        {
            "hours_set": bool(slots),
            "slots": [s.isoformat() for s in slots],
        }
    )


@api_view(["POST"])
@permission_classes([AllowAny])
@throttle_classes([ViewingRequestThrottle])
def public_viewing_request(request):
    """
    POST {property, name, email, phone?, requested_time (ISO), message?}
    -> creates a REQUESTED viewing. Returns a minimal ack (no internal ids
    beyond a reference, no landlord data).
    """
    data = request.data
    prop = _public_property_or_404(data.get("property"))

    name = (data.get("name") or "").strip()
    email = (data.get("email") or "").strip()

    if len(name) < 2:
        raise ValidationError({"name": "Please tell the landlord who you are."})
    if "@" not in email or "." not in email.split("@")[-1]:
        raise ValidationError(
            {"email": "A valid email is required so the landlord can reply."}
        )

    when = parse_datetime(str(data.get("requested_time") or ""))
    if not when:
        raise ValidationError({"requested_time": "Pick a date and time."})
    if timezone.is_naive(when):
        when = timezone.make_aware(when)
    if when <= timezone.now():
        raise ValidationError({"requested_time": "Pick a time in the future."})

    open_count = Appointment.objects.filter(
        property=prop, status=Appointment.Status.REQUESTED
    ).count()
    if open_count >= MAX_OPEN_REQUESTS_PER_PROPERTY:
        raise ValidationError(
            {"detail": "This property isn't accepting more viewing requests right now."}
        )

    # If someone lives there now, the showing is their entry notice: link the
    # lease and route the request to them for consent (advisory — the landlord
    # can still confirm). A vacant unit has no one to ask.
    active_lease = current_active_lease(prop)
    message = (data.get("message") or "").strip()

    appt = Appointment.objects.create(
        landlord=prop.landlord,
        property=prop,
        lease=active_lease,
        kind=Appointment.Kind.VIEWING,
        status=Appointment.Status.REQUESTED,
        starts_at=when,
        contact_name=name,
        contact_email=email,
        contact_phone=(data.get("phone") or "").strip(),
        notes=message,
        tenant_consent=(
            Appointment.TenantConsent.PENDING
            if active_lease
            else Appointment.TenantConsent.NOT_APPLICABLE
        ),
    )
    appt.stamp_time_class()
    appt.save(update_fields=["time_class"])
    appt.record_proposal(
        by=AppointmentProposal.By.REQUESTER, starts_at=when, message=message
    )
    appt.publish_event("appointment.requested")
    if active_lease:
        appt.publish_event("appointment.tenant_review")

    return Response(
        {
            "ok": True,
            "reference": str(appt.pk)[:8].upper(),
            # The requester's capability link — the SAME url the confirmation
            # email carries, returned here too so the thank-you screen can
            # offer "track your request" immediately.
            "status_token": str(appt.public_token),
            "detail": "Request sent — the landlord will confirm or propose another time by email.",
        },
        status=201,
    )


@api_view(["POST"])
@permission_classes([AllowAny])
@throttle_classes([ViewingRequestThrottle])
def public_viewing_respond(request, token):
    """
    POST /api/public/viewing-respond/<token>/
        {action: "accept" | "counter" | "withdraw", requested_time?, message?}

    The requester's half of the negotiation — no account, capability token only.
    - accept:   the landlord proposed a time; take it → SCHEDULED.
    - counter:  suggest a different time → back to the landlord (REQUESTED).
    - withdraw: drop the request → CANCELLED.
    """
    try:
        appt = Appointment.objects.select_related("property", "landlord").get(
            public_token=token, kind=Appointment.Kind.VIEWING
        )
    except (Appointment.DoesNotExist, ValueError, ValidationError):
        raise NotFound("No viewing request found for this link.")

    action = str(request.data.get("action") or "").strip().lower()

    if action == "withdraw":
        if appt.status in (Appointment.Status.CANCELLED, Appointment.Status.COMPLETED):
            raise ValidationError({"detail": "This request is already closed."})
        appt.transition_to(Appointment.Status.CANCELLED)
        appt.publish_event("appointment.cancelled", cancelled_by="REQUESTER")
        return Response({"ok": True, "status": appt.status})

    if appt.status != Appointment.Status.AWAITING_REQUESTER:
        raise ValidationError(
            {"detail": "There's nothing awaiting your reply on this request right now."}
        )

    if action == "accept":
        appt.transition_to(Appointment.Status.SCHEDULED)
        appt.publish_event("appointment.scheduled")
        return Response({"ok": True, "status": appt.status})

    if action == "counter":
        when = parse_datetime(str(request.data.get("requested_time") or ""))
        if not when:
            raise ValidationError({"requested_time": "Pick a date and time."})
        if timezone.is_naive(when):
            when = timezone.make_aware(when)
        if when <= timezone.now():
            raise ValidationError({"requested_time": "Pick a time in the future."})

        appt.starts_at = when
        appt.stamp_time_class()
        # A new time means the current tenant (if any) must be re-asked.
        if appt.lease_id:
            appt.tenant_consent = Appointment.TenantConsent.PENDING
        appt.transition_to(Appointment.Status.REQUESTED, by=None)
        appt.save(update_fields=["starts_at", "time_class", "tenant_consent"])
        appt.record_proposal(
            by=AppointmentProposal.By.REQUESTER,
            starts_at=when,
            message=(request.data.get("message") or "").strip(),
        )
        appt.publish_event("appointment.countered", proposed_by="REQUESTER")
        if appt.lease_id:
            appt.publish_event("appointment.tenant_review")
        return Response({"ok": True, "status": appt.status})

    raise ValidationError({"action": "Use accept, counter, or withdraw."})


@api_view(["GET"])
@permission_classes([AllowAny])
@throttle_classes([ViewingRequestThrottle])
def public_viewing_status(request, token):
    """
    GET /api/public/viewing-status/<token>/

    The requester's status page. The token is a per-appointment capability:
    whoever holds it can read THIS appointment's state and nothing else —
    no account needed, no enumeration possible (uuid4), and the payload
    stays as privacy-safe as the teaser (no street address, no landlord
    contact details beyond what the confirmation email already carries).
    """
    try:
        appt = Appointment.objects.select_related("property").get(
            public_token=token, kind=Appointment.Kind.VIEWING
        )
    except (Appointment.DoesNotExist, ValueError, ValidationError):
        raise NotFound("No viewing request found for this link.")

    prop = appt.property
    return Response(
        {
            "status": appt.status,
            "status_display": appt.get_status_display(),
            "starts_at": appt.starts_at.isoformat(),
            "requested_by": appt.contact_name,
            "property": {
                "name": prop.name,
                "location": prop.public_location,
                "city": prop.city,
                "province": prop.province_code,
                "type_label": prop.public_type_label,
            },
        }
    )
