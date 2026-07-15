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

from ..models import Appointment

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

    appt = Appointment.objects.create(
        landlord=prop.landlord,
        property=prop,
        kind=Appointment.Kind.VIEWING,
        status=Appointment.Status.REQUESTED,
        starts_at=when,
        contact_name=name,
        contact_email=email,
        contact_phone=(data.get("phone") or "").strip(),
        notes=(data.get("message") or "").strip(),
    )
    appt.publish_event("appointment.requested")

    return Response(
        {
            "ok": True,
            "reference": str(appt.pk)[:8].upper(),
            "detail": "Request sent — the landlord will confirm or propose another time by email.",
        },
        status=201,
    )
