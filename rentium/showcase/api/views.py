"""
Public (AllowAny + throttled) reads, plus the landlord's own showcase settings,
inquiry inbox, and address autocomplete.

The project-wide DEFAULT_PERMISSION_CLASSES is IsAuthenticated, so every AllowAny
in this file is an explicit, deliberate act. That's the property we want: you
cannot accidentally publish an endpoint in this codebase — you have to mean it,
and it has to be visible in a diff.
"""

from django.utils.text import slugify
from rest_framework import status
from rest_framework import viewsets
from rest_framework.decorators import action
from rest_framework.decorators import api_view
from rest_framework.decorators import permission_classes
from rest_framework.decorators import throttle_classes
from rest_framework.exceptions import NotFound
from rest_framework.exceptions import PermissionDenied
from rest_framework.exceptions import ValidationError
from rest_framework.permissions import AllowAny
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.throttling import AnonRateThrottle
from rest_framework.throttling import ScopedRateThrottle
from rest_framework.throttling import UserRateThrottle

from django.conf import settings

from rentium.core.geo import GeoError
from rentium.core.geo import autocomplete
from rentium.properties.models import Property


def _subdomain_url(slug):
    """The landlord's vanity showcase URL, e.g. https://raj.rentium.ca. Derived
    from PUBLIC_SITE_URL's host so it follows the deployment without a second
    config knob. Returns None until they've chosen a slug."""
    if not slug:
        return None
    from urllib.parse import urlsplit

    base = getattr(settings, "PUBLIC_SITE_URL", "") or getattr(
        settings, "FRONTEND_URL", ""
    )
    parts = urlsplit(base or "https://rentium.ca")
    host = parts.hostname or "rentium.ca"
    host = host[4:] if host.startswith("www.") else host
    scheme = parts.scheme or "https"
    port = f":{parts.port}" if parts.port else ""
    return f"{scheme}://{slug}.{host}{port}"

from .. import services
from ..models import Inquiry
from ..models import Showcase
from .serializers import InquiryCreateSerializer
from .serializers import InquirySerializer
from .serializers import PublicPropertyCardSerializer
from .serializers import PublicPropertyDetailSerializer
from .serializers import PublicShowcaseSerializer


class PublicReadThrottle(AnonRateThrottle):
    scope = "public_read"


class InquiryThrottle(ScopedRateThrottle):
    scope = "inquiry"


class AddressSearchThrottle(UserRateThrottle):
    scope = "address_search"


def _client_ip(request):
    fwd = request.META.get("HTTP_X_FORWARDED_FOR")
    return fwd.split(",")[0].strip() if fwd else request.META.get("REMOTE_ADDR")


# ------------------------------------------------------------------- cities
@api_view(["GET"])
@permission_classes([AllowAny])
@throttle_classes([PublicReadThrottle])
def public_city(request, province, city):
    """
    GET /api/public/cities/<province>/<city>/?type=&furnished=&min_rent=&max_rent=

    Returns 200 with an EMPTY results list when the city is known but has no
    vacancies right now — and that is the whole reason known_city() exists.

    A city page that 404s the moment its rooms fill up gets dropped from Google's
    index, and then has to climb back from nothing every single time inventory
    turns over. For rentals, inventory turns over constantly. A page whose entire
    value is "here are today's listings" is structurally guaranteed to spend most
    of its life deindexed. So the page survives the gap, and the frontend renders
    its evergreen content around an empty grid.
    """
    known = services.known_city(province, city)
    if not known:
        raise NotFound("We don't have any properties in that city yet.")

    qs = services.city_properties(province, city)

    # Facets describe the UNFILTERED market, so the filter chips can honestly say
    # "3 private rooms" while you're currently looking at the shared ones.
    facets = services.city_facets(qs)

    kind = request.query_params.get("type")
    if kind == "private_room":
        qs = qs.filter(
            property_category=Property.PropertyCategory.ROOM,
            room_type=Property.RoomType.PRIVATE,
        )
    elif kind == "shared_room":
        qs = qs.filter(
            property_category=Property.PropertyCategory.ROOM,
            room_type=Property.RoomType.SHARED,
        )
    elif kind == "full_suite":
        qs = qs.filter(property_category=Property.PropertyCategory.COMPLETE_UNIT)

    if request.query_params.get("furnished") in ("1", "true", "yes"):
        qs = qs.filter(is_furnished=True)

    for param, lookup in (
        ("min_rent", "asking_rent__gte"),
        ("max_rent", "asking_rent__lte"),
    ):
        raw = request.query_params.get(param)
        if raw:
            try:
                qs = qs.filter(**{lookup: float(raw)})
            except ValueError:
                raise ValidationError({param: "Must be a number."})

    return Response(
        {
            **known,
            "facets": facets,
            "results": PublicPropertyCardSerializer(
                qs, many=True, context={"request": request}
            ).data,
        }
    )


@api_view(["GET"])
@permission_classes([AllowAny])
@throttle_classes([PublicReadThrottle])
def public_cities_index(request):
    return Response({"cities": services.all_public_cities()})


# ---------------------------------------------------------------- showcases
@api_view(["GET"])
@permission_classes([AllowAny])
@throttle_classes([PublicReadThrottle])
def public_showcase(request, slug):
    """
    GET /api/public/l/<slug>/

    A live slug returns the page. A RETIRED slug returns {"redirect_to": "..."} so
    the frontend can 301 — a landlord who tidies up their URL shouldn't 404 every
    link they've already put on a poster, and a redirect carries the ranking the
    old URL earned. A slug belonging to a landlord who has since opted OUT returns
    404, not a redirect: turning your page off has to actually turn it off.
    """
    showcase, redirect_to = services.resolve_showcase(slug)
    if not showcase:
        raise NotFound("No public page at that address.")
    if redirect_to:
        return Response({"redirect_to": redirect_to}, status=status.HTTP_200_OK)
    return Response(
        PublicShowcaseSerializer(showcase, context={"request": request}).data
    )


# ----------------------------------------------------------------- property
@api_view(["GET"])
@permission_classes([AllowAny])
@throttle_classes([PublicReadThrottle])
def public_property_detail(request, slug):
    prop = (
        Property.objects.public()
        .filter(public_slug=slug)
        .select_related("landlord__showcase", "landlord__user", "group")
        .prefetch_related("property_images", "inventory_items")
        .first()
    )
    if not prop:
        raise NotFound("This place isn't available.")
    return Response(
        PublicPropertyDetailSerializer(prop, context={"request": request}).data
    )


# ---------------------------------------------------------------- inquiries
@api_view(["POST"])
@permission_classes([AllowAny])
@throttle_classes([InquiryThrottle])
def public_inquiry(request):
    """POST /api/public/inquiries/ — the contact form. No account required."""
    payload = InquiryCreateSerializer(data=request.data)
    payload.is_valid(raise_exception=True)

    prop = payload.validated_data.pop("_property")
    payload.validated_data.pop("property_slug", None)
    payload.validated_data.pop("website", None)  # honeypot

    inquiry = Inquiry.objects.create(
        property=prop,
        landlord=prop.landlord,
        source_ip=_client_ip(request),
        user_agent=request.META.get("HTTP_USER_AGENT", "")[:300],
        **payload.validated_data,
    )

    # Merge the inquiry into a continuing prospect thread and post the message
    # into it, so the landlord replies in one place and the prospect can keep
    # the conversation going from their tokenized chat link.
    from rentium.messaging.services import get_or_create_lead_thread, post_prospect_message

    conversation = get_or_create_lead_thread(inquiry)
    inquiry.conversation = conversation
    inquiry.save(update_fields=["conversation"])
    if inquiry.message:
        # Seed the thread silently — inquiry.created (below) already tells the
        # landlord, so we don't want a second "new message" row for one lead.
        post_prospect_message(conversation, inquiry.message, notify=False)

    inquiry.publish_event()  # -> in-app notification + email to the landlord

    return Response(
        {
            "ok": True,
            "detail": (
                "Sent. The landlord has your message and will reply to you by email."
            ),
        },
        status=status.HTTP_201_CREATED,
    )


# ------------------------------------------------------------------ sitemap
@api_view(["GET"])
@permission_classes([AllowAny])
@throttle_classes([PublicReadThrottle])
def sitemap_data(request):
    """
    Everything the Next.js sitemap.ts needs, in one call.

    Because it reads live from Property.objects.public(), the sitemap regenerates
    as properties change with no rebuild step and no cron job that someone
    eventually forgets to run: a room going OCCUPIED drops out of it on the next
    revalidate, because the status automation already dropped it out of the public
    queryset. The listings self-clean, so the sitemap does too.
    """
    props = (
        Property.objects.public()
        .values("public_slug", "city_slug", "province_code", "updated_at")
        .order_by("-updated_at")
    )
    showcases = Showcase.objects.filter(is_public=True).values("slug", "updated_at")

    return Response(
        {
            "cities": services.all_public_cities(),
            "properties": list(props),
            "showcases": [s for s in showcases if s["slug"]],
        }
    )


# ============================================================== LANDLORD SIDE
@api_view(["GET"])
@permission_classes([IsAuthenticated])
@throttle_classes([AddressSearchThrottle])
def address_search(request):
    """
    GET /api/showcase/address-search/?q=3213+wasca

    -> {"results": [{label: "3213 Wascana Street, Victoria, BC V8Z 3T7",
                     address, city, province, province_code, postal_code,
                     neighbourhood, latitude, longitude}]}

    The landlord types a street address and picks one from the list. City,
    province, postal code, neighbourhood and coordinates all arrive WITH it, so
    those fields are derived rather than typed.

    That is the entire point of this endpoint. The old form asked for city and
    province as free text, which produced three separate "Victoria"s (splitting
    the one URL we're trying to rank across three) and — worse — silently made a
    property permanently unpublishable the moment anyone typed "Britsh Columbia",
    with no error shown anywhere, ever. A field you don't ask for is a field
    nobody can typo.

    Authenticated and throttled because it proxies a metered API. The Geoapify key
    stays server-side: a key in the browser is a key strangers spend, and 3,000
    requests a day is a budget a bot burns in an afternoon.
    """
    query = request.query_params.get("q", "")
    if len(query.strip()) < 3:
        return Response({"results": []})

    try:
        results = autocomplete(query)
    except GeoError as exc:
        # 503, not 500. "The address service is down" is a different thing from
        # "your form is broken", and the UI says so rather than blaming the user.
        return Response(
            {"detail": str(exc)}, status=status.HTTP_503_SERVICE_UNAVAILABLE
        )

    return Response({"results": results})


class ShowcaseSettingsViewSet(viewsets.ViewSet):
    """
    /api/showcase/settings/                      GET
    /api/showcase/settings/update_settings/      PATCH
    /api/showcase/settings/check_slug/?slug=x    GET
    """

    permission_classes = [IsAuthenticated]

    def _showcase(self, request):
        if not hasattr(request.user, "landlord_profile"):
            raise PermissionDenied("Landlords only.")
        # Lazily created, and created PRIVATE. Making the row is bookkeeping;
        # consent is a separate, explicit act.
        return Showcase.for_landlord(request.user.landlord_profile)

    def _payload(self, showcase, request):
        landlord = showcase.landlord

        # Properties that WOULD be public but can't be — no price, no photo, an
        # address we couldn't place on a map.
        #
        # This list exists because the old failure was SILENT. A landlord flipped
        # the switch, saw three of their five properties appear, and had no way
        # whatsoever to find out why the other two didn't. Now they're named, with
        # reasons, in words they can act on.
        blocked = []
        for prop in Property.objects.filter(
            landlord=landlord,
            is_publicly_visible=True,
            status=Property.PropertyStatus.AVAILABLE,
        ):
            reasons = prop.publish_blockers()
            if reasons:
                blocked.append({"id": prop.pk, "name": prop.name, "reasons": reasons})

        return {
            "is_public": showcase.is_public,
            "slug": showcase.slug,
            "display_name": showcase.display_name,
            "bio": showcase.bio,
            "photo": request.build_absolute_uri(showcase.photo.url)
            if showcase.photo
            else None,
            "contact_email": showcase.contact_email,
            "effective_contact_email": showcase.inquiry_email,
            # Canonical path form (the SEO anchor); the vanity subdomain is a
            # mirror the frontend can show a landlord as their shareable link.
            "public_url": f"/l/{showcase.slug}" if showcase.slug else None,
            "subdomain_url": _subdomain_url(showcase.slug),
            "public_property_count": showcase.public_properties().count()
            if showcase.is_public
            else 0,
            "hidden_property_count": Property.objects.filter(
                landlord=landlord, is_publicly_visible=False
            ).count(),
            "blocked_properties": blocked,
        }

    def list(self, request):
        return Response(self._payload(self._showcase(request), request))

    @action(detail=False, methods=["patch"])
    def update_settings(self, request):
        showcase = self._showcase(request)
        data = request.data

        if "slug" in data:
            new_slug = slugify(str(data["slug"] or ""))[:60]
            if not services.slug_is_available(new_slug, exclude_showcase=showcase):
                raise ValidationError({"slug": "That URL isn't available."})
            # Retires the old slug into history so it keeps redirecting.
            services.rename_slug(showcase, new_slug)

        for field in ("display_name", "bio", "contact_email"):
            if field in data:
                setattr(showcase, field, data[field] or "")

        if "photo" in request.FILES:
            showcase.photo = request.FILES["photo"]

        if "is_public" in data:
            wants_public = str(data["is_public"]).lower() in ("1", "true", "yes")
            if wants_public and not showcase.slug:
                raise ValidationError(
                    {"slug": "Choose your public URL before turning your page on."}
                )
            showcase.is_public = wants_public

        showcase.save()
        return Response(self._payload(showcase, request))

    @action(detail=False, methods=["get"])
    def check_slug(self, request):
        showcase = self._showcase(request)
        clean = slugify(request.query_params.get("slug", ""))[:60]
        return Response(
            {
                "slug": clean,
                "available": services.slug_is_available(
                    clean, exclude_showcase=showcase
                ),
            }
        )


class InquiryViewSet(viewsets.ModelViewSet):
    """The landlord's inquiry inbox."""

    serializer_class = InquirySerializer
    permission_classes = [IsAuthenticated]
    http_method_names = ["get", "patch", "post", "head", "options"]

    def get_queryset(self):
        user = self.request.user
        if not hasattr(user, "landlord_profile"):
            return Inquiry.objects.none()

        qs = Inquiry.objects.filter(landlord=user.landlord_profile).select_related(
            "property"
        )
        wanted = self.request.query_params.get("status")
        if wanted:
            qs = qs.filter(status=wanted)
        return qs

    @action(detail=True, methods=["post"])
    def mark_replied(self, request, pk=None):
        inquiry = self.get_object()
        inquiry.mark_replied()
        return Response(self.get_serializer(inquiry).data)

    @action(detail=True, methods=["post"])
    def to_appointment(self, request, pk=None):
        """
        Turn an inquiry into a confirmed viewing in one click, carrying the
        person's name, email and phone across so the landlord retypes nothing.

        This is why Inquiry and Appointment are separate models that link, rather
        than one model doing both jobs: an inquiry is a lead (it may go nowhere),
        an appointment is a commitment (tenants currently in the property get an
        entry notice from it). Conflating them would mean every idle enquiry
        notified somebody's tenants.

        Body: {starts_at: ISO}
        """
        from django.utils.dateparse import parse_datetime

        from rentium.appointments.models import Appointment

        inquiry = self.get_object()
        when = parse_datetime(str(request.data.get("starts_at") or ""))
        if not when:
            raise ValidationError({"starts_at": "Pick a date and time."})

        appt = Appointment.objects.create(
            landlord=inquiry.landlord,
            property=inquiry.property,
            kind=Appointment.Kind.VIEWING,
            status=Appointment.Status.SCHEDULED,
            starts_at=when,
            contact_name=inquiry.name,
            contact_email=inquiry.email,
            contact_phone=inquiry.phone,
            notes=f"From inquiry: {inquiry.message[:200]}",
        )
        appt.publish_event("appointment.scheduled")

        inquiry.appointment = appt
        inquiry.mark_replied()
        inquiry.save(update_fields=["appointment"])

        return Response(self.get_serializer(inquiry).data, status=201)
