from django.contrib import admin

from .models import Property


@admin.register(Property)
class PropertyAdmin(admin.ModelAdmin):
    list_display = (
        "name",
        "address",
        "city",
        "property_category",
        "get_type_display",
        "status",
        "landlord",
    )
    list_filter = ("status", "property_category", "unit_type", "room_type", "city")
    search_fields = ("name", "address", "city", "landlord__user__name")
    readonly_fields = ("created_at", "updated_at")

    def get_type_display(self, obj):
        if obj.property_category == Property.PropertyCategory.COMPLETE_UNIT:
            return obj.get_unit_type_display()
        elif obj.property_category == Property.PropertyCategory.ROOM:
            return obj.get_room_type_display()
        return ""

    get_type_display.short_description = "Type"

    def get_fieldsets(self, request, obj=None):
        common_fieldsets = [
            (
                None,
                {"fields": ("landlord", "name", "description", "property_category")},
            ),
            (
                "Location",
                {"fields": ("address", "city", "province", "postal_code", "country")},
            ),
            ("Status", {"fields": ("status",)}),
            (
                "Timestamps",
                {"fields": ("created_at", "updated_at"), "classes": ("collapse",)},
            ),
        ]

        # Insert category-specific fields after property_category
        if (
            obj is None
            or obj.property_category == Property.PropertyCategory.COMPLETE_UNIT
        ):
            complete_unit_fieldset = (
                "Complete Unit Details",
                {
                    "fields": (
                        "unit_type",
                        "bedrooms",
                        "bathrooms",
                        "max_occupancy",
                        "square_footage",
                    ),
                    "classes": (
                        []
                        if obj
                        and obj.property_category
                        == Property.PropertyCategory.COMPLETE_UNIT
                        else ["collapse"],
                    ),
                },
            )
            common_fieldsets.insert(2, complete_unit_fieldset)

        if obj is None or obj.property_category == Property.PropertyCategory.ROOM:
            room_fieldset = (
                "Room Details",
                {
                    "fields": (
                        "room_type",
                        "total_washrooms",
                        "other_rooms",
                        "shared_with",
                    ),
                    "classes": (
                        []
                        if obj
                        and obj.property_category == Property.PropertyCategory.ROOM
                        else ["collapse"],
                    ),
                },
            )
            common_fieldsets.insert(2, room_fieldset)

        return common_fieldsets

    def get_form(self, request, obj=None, **kwargs):
        form = super().get_form(request, obj, **kwargs)
        if obj:
            # If editing existing object, hide irrelevant fields based on property_category
            if obj.property_category == Property.PropertyCategory.COMPLETE_UNIT:
                for field in [
                    "room_type",
                    "total_washrooms",
                    "other_rooms",
                    "shared_with",
                ]:
                    if field in form.base_fields:
                        form.base_fields[field].widget.attrs["disabled"] = True

            elif obj.property_category == Property.PropertyCategory.ROOM:
                for field in [
                    "unit_type",
                    "bedrooms",
                    "bathrooms",
                    "max_occupancy",
                    "square_footage",
                ]:
                    if field in form.base_fields:
                        form.base_fields[field].widget.attrs["disabled"] = True

        return form
