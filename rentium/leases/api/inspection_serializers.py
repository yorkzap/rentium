# inspection_serializers.py
"""
Serializers for condition inspections. Reads are rich (nested items + keys);
writes are narrow — item/key content flows through the bulk endpoints on the
viewset, signatures and pass transitions through explicit actions, so the
document's rules live in inspection_services, not scattered across PATCHes.
"""

from rest_framework import serializers

from rentium.leases.inspections import (
    CleanlinessCode,
    ConditionCode,
    ConditionInspection,
    InspectionItem,
    InspectionKeyRow,
)


class InspectionItemSerializer(serializers.ModelSerializer):
    area_name = serializers.CharField(source="area.name", read_only=True, allow_null=True)
    work_order_id = serializers.PrimaryKeyRelatedField(
        source="work_order", read_only=True
    )

    class Meta:
        model = InspectionItem
        fields = [
            "id", "section", "label", "sort_order", "is_custom",
            "area", "area_name", "inventory_item", "shared_inventory_item",
            "move_in_condition_code", "move_in_cleanliness_code", "move_in_comment",
            "move_out_condition_code", "move_out_cleanliness_code", "move_out_comment",
            "needs_attention", "suggestion_status", "work_order_id",
        ]
        read_only_fields = [
            "id", "section", "label", "sort_order", "is_custom",
            "area", "area_name", "inventory_item", "shared_inventory_item",
            "suggestion_status", "work_order_id",
        ]


class InspectionKeyRowSerializer(serializers.ModelSerializer):
    class Meta:
        model = InspectionKeyRow
        fields = ["id", "key_type", "issued_count", "returned_count", "sort_order"]
        read_only_fields = ["id", "sort_order"]


class InspectionListSerializer(serializers.ModelSerializer):
    lease_number = serializers.CharField(source="lease.lease_number", read_only=True)
    status_display = serializers.CharField(source="get_status_display", read_only=True)
    tenant_name = serializers.SerializerMethodField()
    property_label = serializers.SerializerMethodField()
    pending_suggestions = serializers.SerializerMethodField()

    class Meta:
        model = ConditionInspection
        fields = [
            "id", "lease", "lease_number", "lease_tenant", "tenant_name",
            "property_label", "status", "status_display",
            "possession_date", "move_in_inspection_date",
            "move_out_date", "move_out_inspection_date",
            "pending_suggestions", "created_at",
        ]

    def get_tenant_name(self, obj):
        lt = obj.lease_tenant
        if not lt:
            return None
        if lt.tenant:
            return lt.tenant.user.name
        return lt.invited_email or None

    def get_property_label(self, obj):
        if obj.lease_tenant and obj.lease_tenant.room:
            return obj.lease_tenant.room.name
        if obj.lease.property:
            return obj.lease.property.name
        return obj.lease.group.name if obj.lease.group else None

    def get_pending_suggestions(self, obj):
        # Cheap on detail/list sizes we have today; add an annotation to the
        # viewset queryset if this ever shows in query counts.
        return obj.items.filter(
            suggestion_status=InspectionItem.SuggestionStatus.PENDING
        ).count()


class InspectionDetailSerializer(InspectionListSerializer):
    items = InspectionItemSerializer(many=True, read_only=True)
    key_rows = InspectionKeyRowSerializer(many=True, read_only=True)
    move_in_fully_signed = serializers.BooleanField(read_only=True)
    move_out_fully_signed = serializers.BooleanField(read_only=True)
    disputed_move_in = serializers.BooleanField(read_only=True)
    disputed_move_out = serializers.BooleanField(read_only=True)
    condition_codes = serializers.SerializerMethodField()
    cleanliness_codes = serializers.SerializerMethodField()

    class Meta(InspectionListSerializer.Meta):
        fields = InspectionListSerializer.Meta.fields + [
            "template",
            "tenant_agent_move_in", "tenant_agent_move_out",
            "repairs_required_at_start", "tenant_responsible_damage",
            "tenant_agrees_move_in", "tenant_disagreement_move_in",
            "tenant_agrees_move_out", "tenant_disagreement_move_out",
            "landlord_signed_move_in_at", "landlord_move_in_signature_name",
            "tenant_signed_move_in_at", "tenant_move_in_signature_name",
            "landlord_signed_move_out_at", "landlord_move_out_signature_name",
            "tenant_signed_move_out_at", "tenant_move_out_signature_name",
            "deduction_security_deposit", "deduction_pet_deposit", "deduction_agreed_at",
            "tenant_forwarding_address",
            "move_in_report_delivered_at", "move_out_report_delivered_at",
            "move_in_fully_signed", "move_out_fully_signed",
            "disputed_move_in", "disputed_move_out",
            "items", "key_rows",
            "condition_codes", "cleanliness_codes",
        ]
        # Header boxes the landlord may PATCH while the relevant pass is open
        # (enforced in the view); signatures/status only via actions.
        read_only_fields = [
            f
            for f in fields
            if f
            not in {
                "possession_date", "move_in_inspection_date",
                "move_out_date", "move_out_inspection_date",
                "tenant_agent_move_in", "tenant_agent_move_out",
                "repairs_required_at_start", "tenant_responsible_damage",
                "tenant_forwarding_address",
            }
        ]

    def get_condition_codes(self, obj):
        """Ship the legend so the frontend never hardcodes it."""
        return [{"value": v, "label": l} for v, l in ConditionCode.choices]

    def get_cleanliness_codes(self, obj):
        return [{"value": v, "label": l} for v, l in CleanlinessCode.choices]


class InspectionCreateSerializer(serializers.Serializer):
    """POST /inspections/ — everything else is derived by build_inspection."""

    lease = serializers.UUIDField()
    lease_tenant = serializers.UUIDField(required=False, allow_null=True)


class ItemBulkRowSerializer(serializers.Serializer):
    """One row of POST /inspections/{id}/items_bulk/."""

    id = serializers.UUIDField()
    move_in_condition_code = serializers.ChoiceField(
        choices=ConditionCode.choices, required=False, allow_blank=True
    )
    move_in_cleanliness_code = serializers.ChoiceField(
        choices=CleanlinessCode.choices, required=False, allow_blank=True
    )
    move_in_comment = serializers.CharField(
        required=False, allow_blank=True, max_length=255
    )
    move_out_condition_code = serializers.ChoiceField(
        choices=ConditionCode.choices, required=False, allow_blank=True
    )
    move_out_cleanliness_code = serializers.ChoiceField(
        choices=CleanlinessCode.choices, required=False, allow_blank=True
    )
    move_out_comment = serializers.CharField(
        required=False, allow_blank=True, max_length=255
    )
    needs_attention = serializers.BooleanField(required=False)


class CustomItemSerializer(serializers.Serializer):
    """POST /inspections/{id}/add_item/ — the paper form's blank lines."""

    section = serializers.CharField(max_length=60)
    label = serializers.CharField(max_length=200)


class KeyBulkRowSerializer(serializers.Serializer):
    id = serializers.UUIDField(required=False, allow_null=True)
    key_type = serializers.CharField(max_length=120)
    issued_count = serializers.IntegerField(min_value=0)
    returned_count = serializers.IntegerField(
        min_value=0, required=False, allow_null=True
    )


class SignSerializer(serializers.Serializer):
    inspection_pass = serializers.ChoiceField(choices=["MOVE_IN", "MOVE_OUT"])
    name = serializers.CharField(max_length=150)
    # Tenant-only (Boxes Y / 1):
    agrees = serializers.BooleanField(required=False, allow_null=True)
    reason = serializers.CharField(required=False, allow_blank=True)