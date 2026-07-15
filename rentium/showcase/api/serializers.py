"""
PUBLIC serializers. Read this file as the privacy contract.

These are deliberately NOT the internal PropertySerializer with fields popped —
that pattern fails open. Someone adds a field to the internal serializer six
months from now, and it silently ships to the open internet without anyone
deciding that it should. These are separate classes with explicit, closed field
lists, in a separate app, so publishing something takes a deliberate act.

The fields that must never appear anywhere below, and the reason isn't
sentimental — a rental listing with a precise address is a rental listing that
tells a stranger which door a person they've never met sleeps behind:

    Property.address, Property.postal_code
    exact latitude / longitude (only the jittered pair is ever emitted)
    landlord's email or phone (contact goes through a form)
    anything from leases, tenants, payments, inspections, or maintenance
"""

from rest_framework import serializers

from rentium.properties.models import Property

from ..models import Inquiry
from ..services import jittered_coords


class PublicImageSerializer(serializers.Serializer):
    url = serializers.SerializerMethodField()
    caption = serializers.CharField()

    def get_url(self, obj):
        request = self.context.get("request")
        try:
            return (
                request.build_absolute_uri(obj.image.url) if request else obj.image.url
            )
        except Exception:
            return None


class PublicPropertyCardSerializer(serializers.ModelSerializer):
    """The grid card on a city page or a landlord's showcase."""

    slug = serializers.CharField(source="public_slug")
    type_label = serializers.CharField(source="public_type_label")
    location = serializers.CharField(source="public_location")
    province = serializers.CharField(source="province_code")
    image = serializers.SerializerMethodField()
    coords = serializers.SerializerMethodField()
    landlord_slug = serializers.SerializerMethodField()

    class Meta:
        model = Property
        fields = [
            "slug",
            "name",
            "type_label",
            "location",
            "city",
            "city_slug",
            "province",
            "asking_rent",
            "available_from",
            "is_furnished",
            "bedrooms",
            "bathrooms",
            "square_footage",
            "image",
            "coords",
            "landlord_slug",
        ]

    def get_image(self, obj):
        request = self.context.get("request")
        if not obj.primary_image:
            return None
        try:
            url = obj.primary_image.url
            return request.build_absolute_uri(url) if request else url
        except Exception:
            return None

    def get_coords(self, obj):
        """
        BLURRED. Never obj.latitude / obj.longitude.

        A precise pin on an otherwise-anonymous listing is a street address with
        extra steps — drop it into Street View and read the house number. The
        offset is deterministic per property (see services.jittered_coords), so
        reloading the page fifty times can't average it away.
        """
        c = jittered_coords(obj)
        return {"lat": c[0], "lng": c[1]} if c else None

    def get_landlord_slug(self, obj):
        showcase = getattr(obj.landlord, "showcase", None)
        return showcase.slug if showcase else None


class PublicPropertyDetailSerializer(PublicPropertyCardSerializer):
    images = serializers.SerializerMethodField()
    furnishings = serializers.SerializerMethodField()
    shared_spaces = serializers.SerializerMethodField()
    building_amenities = serializers.SerializerMethodField()
    landlord = serializers.SerializerMethodField()

    class Meta(PublicPropertyCardSerializer.Meta):
        fields = PublicPropertyCardSerializer.Meta.fields + [
            "description",
            "max_occupancy",
            "images",
            "furnishings",
            "shared_spaces",
            "building_amenities",
            "landlord",
        ]

    def get_images(self, obj):
        return PublicImageSerializer(
            obj.property_images.all(), many=True, context=self.context
        ).data

    def get_furnishings(self, obj):
        """
        What actually comes with the place, straight from the landlord's own
        inventory — so it's never out of date and never typed twice. The same
        list ends up on the roommate agreement and the condition inspection.
        """
        summary = obj.furnishing_summary()
        return {
            "is_furnished": obj.is_furnished,
            "sleeping": summary["sleeping"],
            "furniture": summary["furniture"],
            "appliances": summary["appliances"],
        }

    def get_shared_spaces(self, obj):
        """
        For a ROOM: the suite's common areas, and — crucially — whether the
        landlord is one of the people you'd be sharing them with. That single
        fact determines whether the provincial tenancy act applies to the tenancy
        at all, so someone deciding whether to get in a car and go and view the
        room deserves to know it before they do.

        For a COMPLETE_UNIT: nothing. A unit is self-contained.
        """
        if obj.property_category != Property.PropertyCategory.ROOM or not obj.group_id:
            return []

        from rentium.properties.models import PropertyArea

        areas = (
            PropertyArea.objects.filter(property__group_id=obj.group_id)
            .distinct()
            .order_by("area_type")
        )

        seen, out = set(), []
        for area in areas:
            if area.area_type in seen:
                continue
            seen.add(area.area_type)
            out.append(
                {
                    "name": area.get_area_type_display(),
                    "shared_with_landlord": area.shared_with_landlord,
                }
            )
        return out

    def get_building_amenities(self, obj):
        labels = dict(Property.BuildingAmenity.choices)
        return [labels.get(a, a) for a in (obj.building_amenities or [])]

    def get_landlord(self, obj):
        showcase = getattr(obj.landlord, "showcase", None)
        if not showcase or not showcase.is_public:
            return None

        request = self.context.get("request")
        photo = None
        if showcase.photo:
            try:
                photo = (
                    request.build_absolute_uri(showcase.photo.url)
                    if request
                    else showcase.photo.url
                )
            except Exception:
                photo = None

        # NOTE: no email, no phone. Contact happens through the inquiry form, so
        # a landlord's inbox never ends up scraped off a public page.
        return {"slug": showcase.slug, "name": showcase.public_name, "photo": photo}


class PublicShowcaseSerializer(serializers.Serializer):
    slug = serializers.CharField()
    name = serializers.CharField(source="public_name")
    bio = serializers.CharField()
    photo = serializers.SerializerMethodField()
    properties = serializers.SerializerMethodField()

    def get_photo(self, obj):
        if not obj.photo:
            return None
        request = self.context.get("request")
        try:
            return (
                request.build_absolute_uri(obj.photo.url) if request else obj.photo.url
            )
        except Exception:
            return None

    def get_properties(self, obj):
        return PublicPropertyCardSerializer(
            obj.public_properties(), many=True, context=self.context
        ).data


class InquiryCreateSerializer(serializers.ModelSerializer):
    property_slug = serializers.CharField(write_only=True)
    # Honeypot. A real person never sees this field; bots fill in everything they
    # find. Cheap, silent, and it catches the overwhelming majority of them.
    website = serializers.CharField(required=False, allow_blank=True, write_only=True)

    class Meta:
        model = Inquiry
        fields = [
            "property_slug",
            "name",
            "email",
            "phone",
            "message",
            "move_in_target",
            "website",
        ]

    def validate_name(self, value):
        if len(value.strip()) < 2:
            raise serializers.ValidationError("Please tell the landlord who you are.")
        return value.strip()

    def validate_message(self, value):
        if len(value.strip()) < 10:
            raise serializers.ValidationError(
                "Say a little more — what are you looking for?"
            )
        return value.strip()

    def validate(self, data):
        if data.get("website"):
            raise serializers.ValidationError({"detail": "Submission rejected."})

        # Resolve through .public(), so an inquiry can only ever be sent about a
        # property that is genuinely, currently public. Someone with an old link
        # to a room that has since been rented gets a clean "not available",
        # not a message that quietly reaches a landlord who has no room to offer.
        prop = (
            Property.objects.public()
            .filter(public_slug=data["property_slug"])
            .select_related("landlord")
            .first()
        )
        if not prop:
            raise serializers.ValidationError(
                {"property_slug": "This place isn't available."}
            )

        data["_property"] = prop
        return data


class InquirySerializer(serializers.ModelSerializer):
    """The landlord's OWN view of their inbox (authenticated)."""

    property_name = serializers.CharField(source="property.name", read_only=True)
    property_slug = serializers.CharField(source="property.public_slug", read_only=True)
    status_display = serializers.CharField(source="get_status_display", read_only=True)

    class Meta:
        model = Inquiry
        fields = [
            "id",
            "property",
            "property_name",
            "property_slug",
            "name",
            "email",
            "phone",
            "message",
            "move_in_target",
            "status",
            "status_display",
            "landlord_notes",
            "responded_at",
            "appointment",
            "created_at",
        ]
        # source_ip and user_agent are deliberately absent. They exist for abuse
        # forensics and are nobody's business otherwise — including the
        # landlord's.
        read_only_fields = [f for f in fields if f not in ("status", "landlord_notes")]
