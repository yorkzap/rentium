from rest_framework import serializers

from ..models import Appointment, AppointmentProposal, AvailabilityWindow


class AvailabilityWindowSerializer(serializers.ModelSerializer):
    weekday_display = serializers.CharField(source="get_weekday_display", read_only=True)

    class Meta:
        model = AvailabilityWindow
        fields = ["id", "property", "weekday", "weekday_display", "start_time", "end_time"]

    def validate(self, data):
        start = data.get("start_time") or getattr(self.instance, "start_time", None)
        end = data.get("end_time") or getattr(self.instance, "end_time", None)
        if start and end and end <= start:
            raise serializers.ValidationError(
                {"end_time": "Must be after the start time."}
            )
        return data


class AppointmentProposalSerializer(serializers.ModelSerializer):
    proposed_by_display = serializers.CharField(
        source="get_proposed_by_display", read_only=True
    )

    class Meta:
        model = AppointmentProposal
        fields = ["id", "proposed_by", "proposed_by_display", "starts_at", "message", "created_at"]


class AppointmentSerializer(serializers.ModelSerializer):
    kind_display = serializers.CharField(source="get_kind_display", read_only=True)
    status_display = serializers.CharField(source="get_status_display", read_only=True)
    time_class_display = serializers.CharField(
        source="get_time_class_display", read_only=True
    )
    tenant_consent_display = serializers.CharField(
        source="get_tenant_consent_display", read_only=True
    )
    property_name = serializers.CharField(source="property.name", read_only=True)
    lease_number = serializers.CharField(
        source="lease.lease_number", read_only=True, allow_null=True
    )
    work_order_title = serializers.CharField(
        source="work_order.title", read_only=True, allow_null=True
    )
    proposals = AppointmentProposalSerializer(many=True, read_only=True)

    class Meta:
        model = Appointment
        fields = [
            "id",
            "property",
            "property_name",
            "lease",
            "lease_number",
            "work_order",
            "work_order_title",
            "kind",
            "kind_display",
            "status",
            "status_display",
            "starts_at",
            "ends_at",
            "time_class",
            "time_class_display",
            "tenant_consent",
            "tenant_consent_display",
            "tenant_consent_notes",
            "contact_name",
            "contact_email",
            "contact_phone",
            "notes",
            "prospect_link_first_opened_at",
            "prospect_link_last_opened_at",
            "prospect_link_open_count",
            "proposals",
            "created_at",
            "updated_at",
        ]
        read_only_fields = [
            "id",
            "status",
            "time_class",
            "tenant_consent",
            "prospect_link_first_opened_at",
            "prospect_link_last_opened_at",
            "prospect_link_open_count",
            "created_at",
            "updated_at",
        ]

    def to_representation(self, instance):
        """Tenants see the appointment (their entry notice) but not the
        visitor's private contact details — who is coming and when is their
        business; how to reach that person is the landlord's."""
        data = super().to_representation(instance)
        request = self.context.get("request")
        if request and not hasattr(request.user, "landlord_profile"):
            data["contact_email"] = ""
            data["contact_phone"] = ""
            data["prospect_link_first_opened_at"] = None
            data["prospect_link_last_opened_at"] = None
            data["prospect_link_open_count"] = 0
        return data

    def validate(self, data):
        starts = data.get("starts_at") or getattr(self.instance, "starts_at", None)
        ends = data.get("ends_at") or getattr(self.instance, "ends_at", None)
        if starts and ends and ends <= starts:
            raise serializers.ValidationError(
                {"ends_at": "Must be after the start time."}
            )
        return data
