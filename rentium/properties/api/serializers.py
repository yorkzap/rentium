from rest_framework import serializers

from ..models import Property


class PropertySerializer(serializers.ModelSerializer):
    """
    Serializer for Property model with complete details.
    Used for property creation, updating, and detailed views.
    The 'landlord' field is read-only because it's automatically set
    based on the authenticated user in the view's perform_create method.
    """

    landlord_name = serializers.CharField(source="landlord.user.name", read_only=True)
    # Display fields are useful for detail view
    unit_type_display = serializers.CharField(
        source="get_unit_type_display", read_only=True
    )
    room_type_display = serializers.CharField(
        source="get_room_type_display", read_only=True
    )
    property_category_display = serializers.CharField(
        source="get_property_category_display", read_only=True
    )
    status_display = serializers.CharField(source="get_status_display", read_only=True)

    class Meta:
        model = Property
        fields = [
            "id",
            "landlord",
            "landlord_name",
            "name",
            "description",
            "address",
            "city",
            "province",
            "postal_code",
            "country",
            "property_category",  # Raw category
            "property_category_display",  # Display category
            # Unit fields
            "unit_type",
            "unit_type_display",
            "bedrooms",
            "bathrooms",
            "max_occupancy",
            "square_footage",
            # Room fields
            "room_type",
            "room_type_display",
            "total_washrooms",
            "other_rooms",
            "shared_with",
            # Common fields
            "status",
            "status_display",
            "created_at",
            "updated_at",
        ]
        read_only_fields = [
            "id",  # ID is typically read-only after creation
            "landlord",
            "landlord_name",
            "unit_type_display",
            "room_type_display",
            "property_category_display",
            "status_display",
            "created_at",
            "updated_at",
        ]
        extra_kwargs = {
            # Ensure bathrooms can be properly serialized if DecimalField causes issues
            "bathrooms": {"coerce_to_string": True},
        }

    def validate(self, data):
        # ... (validation logic remains the same) ...
        property_category = data.get(
            "property_category", getattr(self.instance, "property_category", None)
        )

        if not property_category and not self.instance:
            raise serializers.ValidationError(
                {"property_category": "Property category is required."}
            )

        unit_type = data.get("unit_type", getattr(self.instance, "unit_type", None))
        room_type = data.get("room_type", getattr(self.instance, "room_type", None))
        bedrooms = data.get("bedrooms", getattr(self.instance, "bedrooms", None))
        bathrooms = data.get("bathrooms", getattr(self.instance, "bathrooms", None))
        total_washrooms = data.get(
            "total_washrooms", getattr(self.instance, "total_washrooms", None)
        )

        if property_category == Property.PropertyCategory.COMPLETE_UNIT:
            if unit_type is None:
                raise serializers.ValidationError(
                    {"unit_type": "Unit type is required for complete units."}
                )
            if bedrooms is None:
                raise serializers.ValidationError(
                    {"bedrooms": "Number of bedrooms is required for complete units."}
                )
            if bathrooms is None:
                raise serializers.ValidationError(
                    {"bathrooms": "Number of bathrooms is required for complete units."}
                )
            # Optionally clear room-specific fields if switching category or on create/update
            data["room_type"] = None
            data["total_washrooms"] = None
            data["shared_with"] = data.get(
                "shared_with", ""
            )  # Keep optional text fields if provided?

        elif property_category == Property.PropertyCategory.ROOM:
            if room_type is None:
                raise serializers.ValidationError(
                    {"room_type": "Room type is required for room rentals."}
                )
            if total_washrooms is None:
                raise serializers.ValidationError(
                    {"total_washrooms": "Total washrooms is required for room rentals."}
                )
            # Optionally clear unit-specific fields
            data["unit_type"] = None
            data["bedrooms"] = None
            data["bathrooms"] = None
            # Decide if other_rooms applies to ROOM type or should be cleared
            # data['other_rooms'] = data.get('other_rooms', '')

        return data


class PropertyListSerializer(serializers.ModelSerializer):
    """
    Simplified serializer for Property model list view.
    Includes the raw property_category field.
    """

    landlord_name = serializers.CharField(source="landlord.user.name", read_only=True)
    # Removed property_type = serializers.SerializerMethodField()
    status_display = serializers.CharField(source="get_status_display", read_only=True)
    # Optionally add unit/room type display for context if desired
    type_display = serializers.SerializerMethodField()

    class Meta:
        model = Property
        fields = [
            "id",
            "name",
            "address",
            "city",
            "property_category",  # <-- ADDED raw category field
            "type_display",  # <-- ADDED Optional display field for type
            "bedrooms",  # Keep useful summary fields
            "status",
            "status_display",
            "landlord_name",
        ]
        read_only_fields = [
            "id",
            "landlord_name",
            "status_display",
            "type_display",  # <-- ADDED
        ]

    # Removed get_property_type method

    def get_type_display(self, obj):
        """Return display name for unit_type or room_type based on category"""
        if obj.property_category == Property.PropertyCategory.COMPLETE_UNIT:
            return obj.get_unit_type_display()
        elif obj.property_category == Property.PropertyCategory.ROOM:
            return obj.get_room_type_display()
        return None
