from django.db.models import Q
from rest_framework import serializers

from rentium.leases.models import Lease
from rentium.properties.models import PropertyArea
from rentium.properties.models import Property

from ..models import WorkOrder, WorkOrderComment, WorkOrderImage


class AreaSerializer(serializers.ModelSerializer):
    kind_display = serializers.CharField(source="get_kind_display", read_only=True)
    # The landlord's name for the space, falling back to the area type.
    label = serializers.CharField(read_only=True)

    class Meta:
        model = PropertyArea
        fields = [
            "id", "name", "label", "kind", "kind_display", "area_type",
            "exclusive_to", "unit", "group", "property",
        ]
        read_only_fields = fields


class WorkOrderImageSerializer(serializers.ModelSerializer):
    class Meta:
        model = WorkOrderImage
        fields = ["id", "image", "caption", "created_at"]
        read_only_fields = ["id", "created_at"]


class WorkOrderCommentSerializer(serializers.ModelSerializer):
    author_name = serializers.SerializerMethodField()
    is_landlord = serializers.SerializerMethodField()

    class Meta:
        model = WorkOrderComment
        fields = ["id", "body", "author_name", "is_landlord", "created_at"]
        read_only_fields = ["id", "author_name", "is_landlord", "created_at"]

    def get_author_name(self, obj):
        return obj.author.name if obj.author else "Removed user"

    def get_is_landlord(self, obj):
        return bool(obj.author and hasattr(obj.author, "landlord_profile"))


class WorkOrderSerializer(serializers.ModelSerializer):
    property_name = serializers.CharField(source="property.name", read_only=True)
    property_address = serializers.CharField(source="property.address", read_only=True)
    area_name = serializers.CharField(source="area.name", read_only=True, allow_null=True)
    reported_by_name = serializers.SerializerMethodField()
    category_display = serializers.CharField(source="get_category_display", read_only=True)
    priority_display = serializers.CharField(source="get_priority_display", read_only=True)
    status_display = serializers.CharField(source="get_status_display", read_only=True)
    origin_display = serializers.CharField(source="get_origin_display", read_only=True)
    is_rta_emergency = serializers.BooleanField(read_only=True)
    sla_breached = serializers.BooleanField(read_only=True)
    allowed_transitions = serializers.SerializerMethodField()
    images = WorkOrderImageSerializer(many=True, read_only=True)
    comments = WorkOrderCommentSerializer(many=True, read_only=True)

    property_id = serializers.PrimaryKeyRelatedField(
        queryset=Property.objects.all(), source="property", write_only=True
    )
    area_id = serializers.PrimaryKeyRelatedField(
        queryset=PropertyArea.objects.all(), source="area", write_only=True, required=False, allow_null=True
    )
    lease_id = serializers.PrimaryKeyRelatedField(
        queryset=Lease.objects.all(), source="lease", write_only=True, required=False, allow_null=True
    )

    class Meta:
        model = WorkOrder
        fields = [
            "id", "property_id", "property_name", "property_address",
            "area_id", "area_name", "lease_id",
            "reported_by_name", "origin", "origin_display",
            "title", "description",
            "category", "category_display", "priority", "priority_display",
            "status", "status_display", "allowed_transitions",
            "scheduled_date", "completed_date", "cost",
            "contractor_name", "contractor_phone",
            "sla_due_at", "is_rta_emergency", "sla_breached",
            "images", "comments", "created_at", "updated_at",
        ]
        read_only_fields = [
            "id", "property_name", "property_address", "area_name",
            "reported_by_name", "origin_display", "category_display",
            "priority_display", "status_display", "allowed_transitions",
            "sla_due_at", "is_rta_emergency", "sla_breached",
            "images", "comments", "created_at", "updated_at",
            # status changes go through the `transition` action only (FSM)
            "status", "completed_date",
        ]

    def get_reported_by_name(self, obj):
        return obj.reported_by.name if obj.reported_by else "—"

    def get_allowed_transitions(self, obj):
        return sorted(WorkOrder.TRANSITIONS.get(obj.status, set()))

    def validate(self, data):
        """
        Landlords: only their own properties. Tenants: cannot touch
        landlord-only fields, and can only report on a property they rent
        (checked precisely against room/common/exclusive areas).
        """
        request = self.context["request"]
        user = request.user
        prop = data.get("property") or getattr(self.instance, "property", None)
        area = data.get("area")

        if hasattr(user, "landlord_profile"):
            if prop and prop.landlord != user.landlord_profile:
                raise serializers.ValidationError(
                    {"property_id": "You can only manage work orders on your own properties."}
                )
            return data

        if hasattr(user, "tenant_profile"):
            landlord_only = {"cost", "contractor_name", "contractor_phone",
                             "scheduled_date", "origin"}
            touched = landlord_only.intersection(self.initial_data.keys())
            if touched:
                raise serializers.ValidationError(
                    {f: "Only the landlord can set this field." for f in touched}
                )
            data["origin"] = WorkOrder.Origin.TENANT
            if not self.instance and prop:
                on_lease = (
                    Lease.objects.filter(
                        status__in=[Lease.LeaseStatus.ACTIVE, Lease.LeaseStatus.PENDING_SIGNATURES],
                        lease_tenants__tenant=user.tenant_profile,
                    )
                    .filter(_q_property_or_group(prop))
                    .exists()
                )
                if not on_lease:
                    raise serializers.ValidationError(
                        {"property_id": "You can only report issues for a property you currently rent."}
                    )
                if area:
                    from rentium.properties.areas import areas_for_tenant_room

                    if not areas_for_tenant_room(prop).filter(pk=area.pk).exists():
                        raise serializers.ValidationError(
                            {"area_id": "That area isn't part of your rented space."}
                        )
            return data

        raise serializers.ValidationError("Unknown user type.")


def _q_property_or_group(prop):
    q = Q(property=prop)
    if prop.group_id:
        q |= Q(group_id=prop.group_id)
    return q
