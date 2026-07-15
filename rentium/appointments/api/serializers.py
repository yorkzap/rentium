from rest_framework import serializers

from ..models import Appointment


class AppointmentSerializer(serializers.ModelSerializer):
    kind_display = serializers.CharField(source="get_kind_display", read_only=True)
    status_display = serializers.CharField(source="get_status_display", read_only=True)
    property_name = serializers.CharField(source="property.name", read_only=True)
    lease_number = serializers.CharField(
        source="lease.lease_number", read_only=True, allow_null=True
    )
    work_order_title = serializers.CharField(
        source="work_order.title", read_only=True, allow_null=True
    )

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
            "contact_name",
            "contact_email",
            "contact_phone",
            "notes",
            "created_at",
            "updated_at",
        ]
        read_only_fields = ["id", "status", "created_at", "updated_at"]

    def validate(self, data):
        starts = data.get("starts_at") or getattr(self.instance, "starts_at", None)
        ends = data.get("ends_at") or getattr(self.instance, "ends_at", None)
        if starts and ends and ends <= starts:
            raise serializers.ValidationError(
                {"ends_at": "Must be after the start time."}
            )
        return data
